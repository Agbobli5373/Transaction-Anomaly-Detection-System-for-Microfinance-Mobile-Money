"""Model monitoring: PSI against the bundle's training baseline, Alert volume against the budget,
label-based performance on matured Transactions, and where Fraud discoveries came from."""

import dataclasses
import pickle
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import sqlalchemy as sa
from degraded_helpers import StubBundle, drain
from market_helpers import CONFIG, RULES, START, Market, build_market, train
from pydantic import ValidationError
from scoring_helpers import NOW, REGISTERED, SUBJECT, tx
from test_degraded import Scoring, world  # noqa: F401  (the fixture)

from txrisk.cli import main
from txrisk.evaluation import Split, load_evaluation_config, split_of
from txrisk.generator import GeneratorConfig, load_config
from txrisk.models import INPUT_COLUMNS, model_inputs
from txrisk.models.bundle import ModelBundle, train_bundle
from txrisk.monitoring import (
    Baseline,
    Distribution,
    fit_baseline,
    fit_distribution,
    load_monitoring_config,
    psi,
)
from txrisk.monitoring import job as monitor
from txrisk.monitoring.job import Monitored, monitor_day
from txrisk.monitoring.metrics import Metric
from txrisk.scoring import RiskAssessment, RiskBand, rule_scores_table
from txrisk.store import FraudLabels, SimulationClock, create_engine, records

ROOT = Path(__file__).resolve().parents[1]
MONITORING = load_monitoring_config(ROOT / "config" / "monitoring.toml")
EVALUATION = load_evaluation_config(ROOT / "config" / "evaluation.toml")
DAY = timedelta(days=1)
# The monitored day, and the moment it ends: the default time labels are read as of.
TODAY = date(2030, 8, 1)
END = datetime(2030, 8, 2, tzinfo=UTC)
ENHANCED = "w-enhanced"


# --- PSI maths ---------------------------------------------------------------------------------


def test_psi_of_a_distribution_against_itself_is_zero():
    values = np.random.default_rng(1).normal(size=5000)
    baseline = fit_distribution(values, bins=10)

    assert psi(baseline, values) == pytest.approx(0.0, abs=1e-9)


def test_psi_of_known_shares_matches_the_formula():
    baseline = Distribution(edges=(0.0,), shares=(0.5, 0.5, 0.0))
    live = [-1.0] * 25 + [1.0] * 75

    expected = (0.25 - 0.5) * np.log(0.25 / 0.5) + (0.75 - 0.5) * np.log(0.75 / 0.5)
    assert psi(baseline, live) == pytest.approx(expected)
    assert expected == pytest.approx(0.2747, abs=1e-4)


def test_psi_grows_with_the_size_of_the_shift():
    rng = np.random.default_rng(2)
    baseline = fit_distribution(rng.normal(size=20_000), bins=10)

    shifts = [psi(baseline, rng.normal(loc=m, size=20_000)) for m in (0.0, 0.4, 1.0)]

    assert shifts[0] < 0.01
    assert 0.1 < shifts[1] < 0.25
    assert shifts[2] > 0.25


def test_missing_values_are_a_bin_of_their_own():
    baseline = fit_distribution([1.0, 2.0, 3.0, 4.0] * 50, bins=4)
    more_missing = [1.0, 2.0, 3.0, 4.0] * 25 + [float("nan")] * 100

    assert baseline.shares[-1] == 0.0
    assert psi(baseline, more_missing) > 0.25
    assert psi(baseline, [float("nan")] * 10) > 0.25


def test_a_bin_nobody_falls_in_does_not_make_psi_infinite():
    baseline = fit_distribution(list(range(100)), bins=10)

    assert np.isfinite(psi(baseline, [5.0] * 100))


def test_a_baseline_stays_the_same_size_however_many_rows_made_it():
    rng = np.random.default_rng(3)

    small = fit_distribution(rng.normal(size=1_000), bins=10)
    large = fit_distribution(rng.normal(size=200_000), bins=10)

    assert len(small.edges) == len(large.edges) == 9
    assert len(large.shares) == 11
    assert sum(large.shares) == pytest.approx(1.0)


def test_a_column_with_repeated_values_gets_fewer_bins():
    mostly_zero = [0.0] * 90 + list(range(1, 11))

    distribution = fit_distribution(mostly_zero, bins=10)

    assert len(set(distribution.edges)) == len(distribution.edges) < 9


# --- the thresholds are config -----------------------------------------------------------------


def test_the_shipped_thresholds_warn_at_a_tenth_and_act_at_a_quarter():
    assert (MONITORING.psi.warn, MONITORING.psi.action) == (0.1, 0.25)


def test_a_warning_threshold_at_or_above_the_action_threshold_is_refused():
    shipped = MONITORING.model_dump()
    shipped["psi"] = {**shipped["psi"], "warn": 0.3}

    with pytest.raises(ValidationError, match="warning threshold"):
        type(MONITORING).model_validate(shipped)


# --- the baseline travels with the bundle ------------------------------------------------------


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return train(market)


def _rows(market: Market, name: Split) -> pd.DataFrame:
    split = split_of(market.transactions["occurred_at"], START, CONFIG)
    rows = (split == name).fillna(False).to_numpy(dtype=bool)
    transactions = market.transactions[rows]
    return model_inputs(transactions, market.features)


def test_a_bundle_carries_the_baseline_of_every_model_input_and_the_risk_score(
    bundle: ModelBundle,
):
    assert bundle.baseline is not None
    assert list(bundle.baseline.inputs) == INPUT_COLUMNS
    assert bundle.baseline.risk_score.edges


def test_the_baseline_also_holds_the_inputs_of_the_threshold_days_high_transactions(
    market: Market, bundle: ModelBundle
):
    """The Alerts' own distribution, which drift in how fraud looks moves."""
    assert bundle.baseline is not None
    transactions = market.transactions[
        (split_of(market.transactions["occurred_at"], START, CONFIG) == Split.THRESHOLDS)
        .fillna(False)
        .to_numpy(dtype=bool)
    ]
    inputs = _rows(market, Split.THRESHOLDS)
    rules = rule_scores_table(transactions, market.features, RULES)
    high = bundle.risk_scores(inputs, rules) >= bundle.thresholds.high

    assert bundle.baseline.alerted_rows == int(high.sum()) > 0
    assert list(bundle.baseline.alerted) == INPUT_COLUMNS
    assert bundle.baseline.alerted["log_amount"] == fit_distribution(
        inputs.loc[high, "log_amount"], bins=10
    )


def test_the_baseline_is_made_of_the_threshold_days_and_nothing_later(
    market: Market, bundle: ModelBundle
):
    """The latest days before going live; the evaluation days, whose drift it is for, are not in."""
    assert bundle.baseline is not None
    thresholds = _rows(market, Split.THRESHOLDS)
    evaluation = _rows(market, Split.EVALUATION)

    assert bundle.baseline.rows == len(thresholds)
    expected = fit_distribution(thresholds["log_amount"], bins=10)
    assert bundle.baseline.inputs["log_amount"] == expected
    assert bundle.baseline.inputs["log_amount"] != fit_distribution(
        evaluation["log_amount"], bins=10
    )


def test_a_baseline_survives_saving_and_loading_the_bundle(bundle: ModelBundle, tmp_path: Path):
    bundle.save(tmp_path)

    loaded = ModelBundle.load(tmp_path)

    assert loaded.baseline == bundle.baseline
    assert loaded.version == bundle.version


def test_the_manifest_lists_the_baseline_and_it_is_small(bundle: ModelBundle, tmp_path: Path):
    bundle.save(tmp_path)

    manifest = (tmp_path / "manifest.json").read_text()

    assert '"baseline"' in manifest and '"risk_score"' in manifest
    assert len(manifest) < 100_000


def test_the_bundle_version_changes_when_the_baseline_changes(market: Market, bundle: ModelBundle):
    coarser = train_bundle(
        market.transactions,
        market.features,
        market.discoveries,
        rules=RULES,
        period_start=START,
        config=CONFIG,
        seed=11,
        baseline_bins=5,
    )

    assert coarser.baseline != bundle.baseline
    assert coarser.version != bundle.version
    # Nothing else about the model changed.
    assert coarser.thresholds == bundle.thresholds


def test_an_old_bundle_without_a_baseline_still_loads_and_has_none(
    bundle: ModelBundle, tmp_path: Path
):
    """A pickle from before baselines has no such attribute at all."""
    old = dataclasses.replace(bundle)
    vars(old).pop("baseline")
    (tmp_path / "bundle.pkl").write_bytes(pickle.dumps(old))

    loaded = ModelBundle.load(tmp_path)

    assert loaded.baseline is None
    assert loaded.version == bundle.version


# --- the model inputs are kept as scored -------------------------------------------------------


def test_a_model_scored_transaction_keeps_its_inputs_and_a_degraded_one_does_not(
    world: str,  # noqa: F811
):
    scoring = Scoring(world, StubBundle())
    scored = drain(NOW)

    scoring.assess(scored)

    with scoring.engine.connect() as connection:
        stored = connection.execute(
            sa.text("SELECT inputs, model_version FROM model_inputs WHERE transaction_id = :t"),
            {"t": scored.transaction_id},
        ).one()
    assert set(stored.inputs) == set(INPUT_COLUMNS)
    assert stored.model_version == "stub-1"
    assert stored.inputs["amount_to_kyc_limit"] is not None

    scoring.stub.fail = True
    degraded = scoring.assess(drain(NOW + timedelta(minutes=5)))
    assert degraded.degraded
    with scoring.engine.connect() as connection:
        kept = connection.execute(sa.text("SELECT count(*) FROM model_inputs")).scalar_one()
    assert kept == 1


def test_rescoring_a_degraded_transaction_stores_its_inputs(world: str):  # noqa: F811
    scoring = Scoring(world, StubBundle(fail=True))
    degraded = drain(NOW)
    scoring.assess(degraded)
    scoring.heal()

    result = scoring.scoring.rescore_degraded()

    assert result.rescored == 1
    with scoring.engine.connect() as connection:
        kept = connection.execute(
            sa.text("SELECT model_version FROM model_inputs WHERE transaction_id = :t"),
            {"t": degraded.transaction_id},
        ).scalar_one()
    assert kept == "stub-1"


# --- what the daily job computes, against a hand-built Postgres --------------------------------


@pytest.fixture
def engine(database: str) -> sa.Engine:
    engine = create_engine(database)
    with engine.begin() as connection:
        records.add_wallet(
            connection, SUBJECT, kyc_tier="minimum", home_region="Ashanti", registered_at=REGISTERED
        )
        records.add_wallet(
            connection,
            ENHANCED,
            kyc_tier="enhanced",
            home_region="Ashanti",
            registered_at=REGISTERED,
        )
        records.add_wallet(
            connection,
            "w-friend",
            kyc_tier="minimum",
            home_region="Ashanti",
            registered_at=REGISTERED,
        )
    return engine


def put(
    engine: sa.Engine,
    at: datetime,
    *,
    band: str = "Low",
    channel: str = "app",
    wallet: str = SUBJECT,
    label: tuple[datetime, str] | None = None,
    inputs: dict[str, float] | None = None,
    score: float = 1.0,
    version: str = "v1",
    degraded: bool = False,
) -> str:
    """A Transaction at `at` with its assessment (and Alert if High), optionally its model inputs
    and a Fraud label."""
    transaction = dataclasses.replace(tx(at, wallet=wallet), channel=channel)
    assessment = RiskAssessment(
        transaction_id=transaction.transaction_id,
        risk_score=score,
        risk_band=RiskBand(band),
        reasons=(),
        model_version=version,
        degraded=degraded,
        model_inputs=inputs,
    )
    with engine.begin() as connection:
        records.record_transaction(connection, transaction)
        records.save_assessment(connection, assessment, assessed_at=at)
        records.save_model_inputs(connection, assessment, assessed_at=at)
        if band == "High":
            records.raise_alert(connection, transaction)
    if label is not None:
        FraudLabels(engine).record(transaction.transaction_id, *label)
    return transaction.transaction_id


def run(
    engine: sa.Engine,
    *,
    baseline: Baseline | None = None,
    day: date = TODAY,
    as_of: datetime | None = None,
    evaluation: Any = EVALUATION,
) -> Monitored:
    return monitor_day(
        engine,
        model_version="v1",
        baseline=baseline,
        config=MONITORING,
        evaluation=evaluation,
        day=day,
        as_of=as_of,
    )


def value(result: Monitored, name: str, dimension: str = "", member: str = "") -> float | None:
    found = [
        m.value
        for m in result.metrics
        if (m.name, m.dimension, m.member) == (name, dimension, member)
    ]
    return found[0] if found else None


def test_psi_is_computed_per_input_and_for_the_risk_score_and_flagged_at_the_thresholds(
    engine: sa.Engine,
):
    rng = random.Random(5)
    reference = pd.DataFrame({"steady": [rng.gauss(0, 1) for _ in range(5000)]})
    reference["shifting"] = [rng.gauss(0, 1) for _ in range(5000)]
    baseline = fit_baseline(reference, pd.Series([rng.random() for _ in range(5000)]), bins=10)
    at = datetime(2030, 8, 1, 12, tzinfo=UTC)
    for _ in range(MONITORING.psi.min_rows):
        put(
            engine,
            at,
            inputs={"steady": rng.gauss(0, 1), "shifting": rng.gauss(1.0, 1)},
            score=rng.random() * 0.2,  # the Risk score has collapsed to a narrow range
        )

    result = run(engine, baseline=baseline)

    assert value(result, "psi", member="steady") < MONITORING.psi.warn
    assert value(result, "psi", member="shifting") >= MONITORING.psi.action
    assert value(result, "psi", member="risk_score") >= MONITORING.psi.action
    assert result.psi_actions == ["shifting", "risk_score"]
    assert result.psi_warnings == []
    assert value(result, "psi_warn_threshold") == 0.1
    assert value(result, "psi_action_threshold") == 0.25


def test_a_shift_between_the_thresholds_is_a_warning_and_not_an_action(engine: sa.Engine):
    rng = random.Random(6)
    baseline = fit_baseline(
        pd.DataFrame({"x": [rng.gauss(0, 1) for _ in range(20_000)]}),
        pd.Series([rng.random() for _ in range(20_000)]),
        bins=10,
    )
    at = datetime(2030, 8, 1, 12, tzinfo=UTC)
    for _ in range(3000):
        put(engine, at, inputs={"x": rng.gauss(0.4, 1)}, score=rng.random())

    result = run(engine, baseline=baseline)

    assert result.psi_warnings == ["x"]
    assert result.psi_actions == []


def test_psi_uses_only_the_champions_scored_transactions_of_the_day(engine: sa.Engine):
    baseline = fit_baseline(pd.DataFrame({"x": [0.0, 1.0] * 500}), pd.Series([0.5] * 1000), bins=2)
    inside = datetime(2030, 8, 1, 12, tzinfo=UTC)
    for _ in range(MONITORING.psi.min_rows):
        put(engine, inside, inputs={"x": 0.0}, score=0.5)
    put(engine, inside - DAY, inputs={"x": 99.0})  # another day
    put(engine, inside, inputs={"x": 99.0}, version="other")  # another model
    put(engine, inside, inputs={"x": 99.0}, degraded=True)  # rules alone

    result = run(engine, baseline=baseline)

    assert value(result, "scored_rows") == MONITORING.psi.min_rows


def test_a_day_with_too_little_to_measure_reports_no_psi(engine: sa.Engine):
    baseline = fit_baseline(pd.DataFrame({"x": [0.0, 1.0]}), pd.Series([0.5, 0.6]), bins=2)
    put(engine, datetime(2030, 8, 1, 12, tzinfo=UTC), inputs={"x": 5.0})

    result = run(engine, baseline=baseline)

    assert value(result, "psi", member="x") is None
    assert value(result, "baseline_available") == 1.0
    assert result.psi_actions == []


def test_a_bundle_without_a_baseline_is_reported_as_no_baseline_not_a_crash(engine: sa.Engine):
    put(engine, datetime(2030, 8, 1, 12, tzinfo=UTC), band="High")

    result = run(engine, baseline=None)

    assert result.baseline_missing
    assert value(result, "baseline_available") == 0.0
    assert not [m for m in result.metrics if m.name == "psi"]
    # Everything that needs no baseline is still computed.
    assert value(result, "alert_volume") == 1


def test_alert_volume_is_compared_with_the_alert_budget_and_its_band(engine: sa.Engine):
    budget = EVALUATION.alert_budget_per_day
    at = datetime(2030, 8, 1, 9, tzinfo=UTC)
    for _ in range(budget // 4):
        put(engine, at, band="High")
    put(engine, at - DAY, band="High")  # yesterday's Alert is not today's

    result = run(engine)

    assert value(result, "alert_volume") == budget // 4
    assert value(result, "alert_budget") == budget
    ratio = value(result, "alert_volume_ratio")
    assert ratio == pytest.approx(0.25)
    assert ratio < MONITORING.alert_volume.min_share_of_budget  # below the band
    assert value(result, "alert_volume_ratio_max") == MONITORING.alert_volume.max_share_of_budget


def test_the_risk_band_mix_counts_the_days_model_assessments(engine: sa.Engine):
    at = datetime(2030, 8, 1, 9, tzinfo=UTC)
    for band in ("Low", "Low", "Low", "Medium", "High"):
        put(engine, at, band=band)
    put(engine, at, band="High", degraded=True)  # on the rules scale: not part of the mix

    result = run(engine)

    assert value(result, "risk_band_count", member="Low") == 3
    assert value(result, "risk_band_share", member="Low") == pytest.approx(0.6)
    assert value(result, "risk_band_share", member="High") == pytest.approx(0.2)


# label-based performance


def matured_at(days_before_end: float) -> datetime:
    return END - timedelta(days=days_before_end)


def test_only_transactions_older_than_the_label_maturity_window_are_judged(engine: sa.Engine):
    maturity = EVALUATION.label_maturity_days
    old = put(engine, matured_at(maturity + 1), band="High")
    just_inside = put(engine, matured_at(maturity - 1), band="High")
    put(engine, matured_at(1), band="High")
    FraudLabels(engine).record(old, matured_at(maturity + 0.5), "complaint")
    FraudLabels(engine).record(just_inside, matured_at(maturity - 0.5), "complaint")

    result = run(engine)

    # Only the first is old enough: the Transaction inside the window does not count, labelled
    # fraud and High as it is.
    assert value(result, "label_transactions", "all", "all") == 1
    assert value(result, "label_positives", "all", "all") == 1
    assert value(result, "label_precision", "all", "all") == 1.0


def test_a_transaction_exactly_at_the_maturity_cutoff_is_still_inside_the_window(
    engine: sa.Engine,
):
    put(engine, matured_at(EVALUATION.label_maturity_days), band="High")

    result = run(engine)

    assert value(result, "label_transactions", "all", "all") is None


def test_only_labels_known_by_the_simulated_time_count(engine: sa.Engine):
    early = put(engine, matured_at(40), band="High")
    late = put(engine, matured_at(41), band="Low")
    FraudLabels(engine).record(early, matured_at(35), "complaint")
    FraudLabels(engine).record(late, END + DAY, "case")  # discovered after the run

    result = run(engine)

    assert value(result, "label_positives", "all", "all") == 1
    assert value(result, "label_recall", "all", "all") == 1.0
    # Run later, the same Transactions and the later label read differently.
    later = run(engine, as_of=END + 2 * DAY)
    assert value(later, "label_positives", "all", "all") == 2
    assert value(later, "label_recall", "all", "all") == 0.5


def test_precision_recall_and_false_positive_rate_at_high_by_segment(engine: sa.Engine):
    at = matured_at(40)
    caught = put(engine, at, band="High", channel="app")
    missed = put(engine, at, band="Low", channel="app")
    put(engine, at, band="High", channel="app")  # a false positive
    for _ in range(5):
        put(engine, at, band="Low", channel="ussd")  # true negatives
    FraudLabels(engine).record(caught, at, "complaint")
    FraudLabels(engine).record(missed, at, "complaint")
    enhanced = put(engine, at, band="High", wallet=ENHANCED, channel="ussd")
    FraudLabels(engine).record(enhanced, at, "case")

    result = run(engine)

    assert value(result, "label_precision", "all", "all") == pytest.approx(2 / 3)
    assert value(result, "label_recall", "all", "all") == pytest.approx(2 / 3)
    assert value(result, "label_fpr", "all", "all") == pytest.approx(1 / 6)
    assert value(result, "label_precision", "channel", "app") == pytest.approx(0.5)
    assert value(result, "label_recall", "channel", "app") == pytest.approx(0.5)
    assert value(result, "label_fpr", "channel", "ussd") == 0.0
    assert value(result, "label_precision", "kyc_tier", "enhanced") == 1.0
    assert value(result, "label_precision", "region", "Ashanti") == pytest.approx(2 / 3)
    assert value(result, "label_transactions", "wallet_age_band", "30-180d") == 9


def test_a_metric_with_nothing_to_divide_is_left_out_not_zero(engine: sa.Engine):
    put(engine, matured_at(40), band="Low")

    result = run(engine)

    assert value(result, "label_precision", "all", "all") is None
    assert value(result, "label_recall", "all", "all") is None
    assert value(result, "label_fpr", "all", "all") == 0.0


def test_a_fall_in_recall_beyond_the_configured_drop_is_reported_against_recent_days(
    engine: sa.Engine,
):
    labels = FraudLabels(engine)

    def one_day(day: date, *, caught: int, missed: int) -> Monitored:
        at = datetime(day.year, day.month, day.day, 12, tzinfo=UTC) - timedelta(days=40)
        for band, count in (("High", caught), ("Low", missed)):
            for _ in range(count):
                labels.record(put(engine, at, band=band), at, "complaint")
        return run(engine, day=day, as_of=datetime(day.year, day.month, day.day, tzinfo=UTC) + DAY)

    first = one_day(TODAY - 2 * DAY, caught=20, missed=0)
    second = one_day(TODAY - DAY, caught=20, missed=0)
    third = one_day(TODAY, caught=10, missed=10)

    assert value(first, "label_drop_recall", "all", "all") is None  # nothing to compare with
    assert value(second, "label_drop_recall", "all", "all") is not None
    # The window is 30 days of matured Transactions, so day three sees all of them.
    assert value(third, "label_drop_recall", "all", "all") > 0
    assert value(third, "label_drop_threshold") == MONITORING.labels.max_relative_drop


def test_a_segment_with_too_few_known_labels_never_reports_a_regression(engine: sa.Engine):
    labels = FraudLabels(engine)
    at = matured_at(40)
    for day in (TODAY - DAY, TODAY):
        for band in ("High", "Low"):
            labels.record(put(engine, at, band=band), at, "complaint")
        result = run(engine, day=day)

    assert value(result, "label_drop_recall", "all", "all") is None


# Fraud discoveries


def test_discoveries_are_split_by_source_and_the_complaint_and_case_shares_add_up(
    engine: sa.Engine,
):
    labels = FraudLabels(engine)
    at = datetime(2030, 8, 1, 10, tzinfo=UTC)
    for source, channel, count in (
        ("complaint", "ussd", 3),
        ("complaint", "app", 1),
        ("case", "app", 2),
        ("chargeback", "app", 2),
    ):
        for _ in range(count):
            labels.record(put(engine, at - DAY, channel=channel), at, source)

    result = run(engine)

    assert value(result, "discoveries_by_source", member="complaint") == 4
    assert value(result, "discovery_share", member="complaint") == pytest.approx(0.5)
    assert value(result, "discovery_share", member="case") == pytest.approx(0.25)
    assert value(result, "discovery_share", member="chargeback") == pytest.approx(0.25)
    assert value(result, "discoveries_by_channel", member="app") == 5
    assert value(result, "discoveries_by_channel", member="ussd") == 3
    assert value(result, "discoveries_day", member="ussd") == 3


def test_recall_against_discovered_labels_counts_scored_fraud_found_at_high(engine: sa.Engine):
    labels = FraudLabels(engine)
    at = datetime(2030, 8, 1, 10, tzinfo=UTC)
    labels.record(put(engine, at, band="High"), at, "complaint")
    labels.record(put(engine, at, band="Medium"), at, "complaint")
    labels.record("T-never-scored", at, "complaint")  # not stored: not ours to have missed
    labels.record(put(engine, at, band="High"), at + 2 * DAY, "complaint")  # not yet known

    result = run(engine)

    assert value(result, "discovered_scored") == 2
    assert value(result, "recall_discovered") == 0.5


# the day's rows


def test_a_days_metrics_are_stored_and_a_rerun_replaces_them(engine: sa.Engine):
    at = datetime(2030, 8, 1, 9, tzinfo=UTC)
    put(engine, at, band="High")
    run(engine)
    put(engine, at, band="High")

    run(engine)

    with engine.connect() as connection:
        stored = connection.execute(
            sa.text(
                "SELECT value FROM monitoring_metrics WHERE day = :d AND metric = 'alert_volume'"
            ),
            {"d": TODAY},
        ).scalars()
        assert list(stored) == [2.0]


def test_every_metric_the_job_computes_has_a_spec_for_prometheus():
    with pytest.raises(ValueError, match="unknown metric"):
        Metric("not_a_metric", 1.0)


def test_the_default_day_is_the_one_before_the_clocks():
    assert monitor.latest_complete_day(datetime(2030, 8, 2, 0, 0, 5, tzinfo=UTC)) == TODAY


# the command


def test_the_monitor_command_monitors_the_day_before_the_clock_by_default(
    database: str, engine: sa.Engine, capsys: pytest.CaptureFixture[str]
):
    put(engine, datetime(2030, 8, 1, 9, tzinfo=UTC), band="High")
    SimulationClock(engine).advance(END + timedelta(seconds=30))

    code = main(["monitor", "--database-url", database])

    assert code == 0
    output = capsys.readouterr().out
    assert "monitored 2030-08-01" in output
    with engine.connect() as connection:
        volume = connection.execute(
            sa.text(
                "SELECT value FROM monitoring_metrics"
                " WHERE metric = 'alert_volume' AND day = '2030-08-01'"
            )
        ).scalar_one()
    assert volume == 1


def test_the_monitor_command_does_nothing_before_the_clock_starts(
    database: str, capsys: pytest.CaptureFixture[str]
):
    assert main(["monitor", "--database-url", database]) == 0
    assert "clock has not started" in capsys.readouterr().out


def test_the_monitor_command_with_an_old_bundle_says_there_is_no_baseline(
    database: str,
    engine: sa.Engine,
    bundle: ModelBundle,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    old = dataclasses.replace(bundle)
    vars(old).pop("baseline")
    (tmp_path / "bundle.pkl").write_bytes(pickle.dumps(old))
    SimulationClock(engine).advance(END + timedelta(seconds=30))

    code = main(["monitor", "--database-url", database, "--bundle", str(tmp_path)])

    assert code == 0
    assert "no baseline" in capsys.readouterr().out


def test_the_monitor_command_refuses_the_application_role(
    database: str, capsys: pytest.CaptureFixture[str]
):
    from api_helpers import APP_CREDENTIAL

    url = sa.make_url(database).set(username="txrisk_app", password=APP_CREDENTIAL)

    code = main(["monitor", "--database-url", url.render_as_string(hide_password=False)])

    assert code == 2
    assert "owner" in capsys.readouterr().err


# --- inputs that only grow, and drift among the Alerts -----------------------------------------


def test_inputs_that_only_grow_with_time_are_not_measured(engine: sa.Engine):
    """A Wallet's history and a device's age grow with the calendar, so they would 'drift' every
    day by construction; the config lists them and the monitor leaves them out."""
    assert {"history_count", "device_age_seconds"} <= set(MONITORING.psi.ignore)
    rng = random.Random(9)
    reference = pd.DataFrame(
        {"history_count": [rng.uniform(0, 10) for _ in range(2000)], "steady": [0.0, 1.0] * 1000}
    )
    baseline = fit_baseline(reference, pd.Series([0.5] * 2000), bins=10)
    at = datetime(2030, 8, 1, 12, tzinfo=UTC)
    for _ in range(MONITORING.psi.min_rows):
        put(engine, at, inputs={"history_count": 500.0, "steady": 0.0}, score=0.5)

    result = run(engine, baseline=baseline)

    assert value(result, "psi", member="history_count") is None
    assert value(result, "psi", member="steady") is not None


# Synthetic Transactions that drift as the generator's do from its drift day: a SIM-swap
# takeover that waited 0.5-12 hours and took 60-100% now waits 24-72 hours and takes 20-40%, and
# a scam appears whose victims are paid by a stranger first. Fraud is a tiny share of everything,
# and all of it is among the High Transactions.
GENERATOR = load_config(ROOT / "config" / "generator.toml")
BEFORE = GENERATOR.fraud.scenarios.sim_swap_takeover
AFTER = GENERATOR.fraud.drift.sim_swap_takeover
assert AFTER is not None


def synthetic_rows(
    rng: np.random.Generator, n: int, *, high: bool, drifted: bool
) -> tuple[pd.DataFrame, np.ndarray]:
    """Model inputs of `n` Transactions, and their Risk scores. High ones are mostly fraud."""
    rows = pd.DataFrame(
        {
            "log_amount": rng.normal(4, 1, n),
            "seconds_since_sim_swap": np.where(
                rng.random(n) < 0.9, np.nan, rng.uniform(86_400, 30 * 86_400, n)
            ),
            "share_of_balance_moved": rng.beta(2, 8, n),
            "received_from_counterparty_24h": np.zeros(n),
        }
    )
    scores = rng.uniform(0.0, 0.02, n)
    if high:
        scores = rng.uniform(0.2, 0.9, n)
        takeover = rng.random(n) < 0.3
        delay = AFTER.delay_hours if drifted else BEFORE.delay_hours
        drain = AFTER.drain_share if drifted else BEFORE.drain_share
        rows.loc[takeover, "seconds_since_sim_swap"] = (
            rng.uniform(delay.low, delay.high, takeover.sum()) * 3600
        )
        rows.loc[takeover, "share_of_balance_moved"] = rng.uniform(
            drain.low, drain.high, takeover.sum()
        )
        if drifted:
            reply = ~takeover & (rng.random(n) < 0.15)
            rows.loc[reply, "received_from_counterparty_24h"] = 1.0
    return rows, scores


def baseline_of_training_days() -> Baseline:
    rng = np.random.default_rng(10)
    everyone, scores = synthetic_rows(rng, 60_000, high=False, drifted=False)
    flagged, flagged_scores = synthetic_rows(rng, 600, high=True, drifted=False)
    return fit_baseline(
        pd.concat([everyone, flagged], ignore_index=True),
        pd.Series(np.concatenate([scores, flagged_scores])),
        bins=10,
        alerted=np.arange(60_600) >= 60_000,
    )


def stock_week(engine: sa.Engine, *, drifted: bool) -> None:
    """Seven days up to TODAY: each with 40 High Transactions, and the last with 1000 others."""
    rng = np.random.default_rng(11)
    for back in range(7):
        at = datetime(2030, 8, 1, 12, tzinfo=UTC) - timedelta(days=back)
        highs, high_scores = synthetic_rows(rng, 40, high=True, drifted=drifted)
        lows, low_scores = (
            synthetic_rows(rng, 1000, high=False, drifted=drifted)
            if back == 0
            else (highs.iloc[:0], np.array([]))
        )
        with engine.begin() as connection:
            for rows, scores, band in ((highs, high_scores, "High"), (lows, low_scores, "Low")):
                for (_, row), score in zip(rows.iterrows(), scores, strict=True):
                    transaction = dataclasses.replace(tx(at), channel="app")
                    assessment = RiskAssessment(
                        transaction_id=transaction.transaction_id,
                        risk_score=float(score),
                        risk_band=RiskBand(band),
                        reasons=(),
                        model_version="v1",
                        model_inputs=row.to_dict(),
                    )
                    records.record_transaction(connection, transaction)
                    records.save_assessment(connection, assessment, assessed_at=at)
                    records.save_model_inputs(connection, assessment, assessed_at=at)


SMALL_SAMPLE = MONITORING.model_copy(
    update={"psi": MONITORING.psi.model_copy(update={"min_rows": 500, "alerted_min_rows": 200})}
)


def test_the_injected_drift_among_the_alerts_is_an_action_while_the_population_stays_quiet(
    engine: sa.Engine,
):
    """The deterministic proof that drift of the kind injected on day 100 raises an action."""
    stock_week(engine, drifted=True)

    result = monitor_day(
        engine,
        model_version="v1",
        baseline=baseline_of_training_days(),
        config=SMALL_SAMPLE,
        evaluation=EVALUATION,
        day=TODAY,
    )

    # The Alerts' SIM-swap timing and the stranger's payment have moved a long way ...
    assert value(result, "psi_alerted", member="seconds_since_sim_swap") >= 0.25
    assert value(result, "psi_alerted", member="received_from_counterparty_24h") >= 0.25
    assert "alerted:seconds_since_sim_swap" in result.psi_actions
    # ... where the whole population's has not, because fraud is a sliver of it.
    for name in ("seconds_since_sim_swap", "share_of_balance_moved", "log_amount", "risk_score"):
        assert value(result, "psi", member=name) < SMALL_SAMPLE.psi.warn, name
    assert not [a for a in result.psi_actions if not a.startswith("alerted:")]


def test_the_same_week_without_the_drift_raises_nothing(engine: sa.Engine):
    stock_week(engine, drifted=False)

    result = monitor_day(
        engine,
        model_version="v1",
        baseline=baseline_of_training_days(),
        config=SMALL_SAMPLE,
        evaluation=EVALUATION,
        day=TODAY,
    )

    assert result.psi_actions == []
    assert value(result, "psi_alerted", member="seconds_since_sim_swap") < MONITORING.psi.warn


def test_the_alerts_window_is_the_trailing_days_and_needs_enough_of_them(engine: sa.Engine):
    rng = np.random.default_rng(12)
    highs, scores = synthetic_rows(rng, 50, high=True, drifted=True)
    for (_, row), score in zip(highs.iterrows(), scores, strict=True):
        put(
            engine,
            datetime(2030, 8, 1, 12, tzinfo=UTC) - 8 * DAY,  # older than the window
            band="High",
            inputs=row.to_dict(),
            score=float(score),
        )

    result = monitor_day(
        engine,
        model_version="v1",
        baseline=baseline_of_training_days(),
        config=SMALL_SAMPLE,
        evaluation=EVALUATION,
        day=TODAY,
    )

    assert value(result, "alerted_rows") == 0
    assert value(result, "psi_alerted", member="seconds_since_sim_swap") is None


# --- retention ---------------------------------------------------------------------------------


def test_the_purge_ages_out_model_inputs_and_monitoring_metrics_by_their_own_periods(
    engine: sa.Engine,
):
    from test_clock_labels_retention import retention

    from txrisk.store import purge

    now = datetime(2030, 8, 1, 12, tzinfo=UTC)
    old = put(engine, now - 40 * DAY, inputs={"x": 1.0})
    new = put(engine, now - 2 * DAY, inputs={"x": 1.0})
    run(engine, day=date(2030, 6, 1))  # a metrics day long ago
    run(engine, day=TODAY)
    SimulationClock(engine).advance(now)

    purged = purge(
        engine,
        SimulationClock(engine),
        retention(
            model_inputs=30, monitoring_metrics=30, transactions=60, risk_assessments=60, alerts=60
        ),
    )

    assert purged is not None
    assert purged.deleted["model_inputs"] == 1
    assert purged.deleted["monitoring_metrics"] > 0
    with engine.connect() as connection:
        left = (
            connection.execute(sa.text("SELECT transaction_id FROM model_inputs")).scalars().all()
        )
        days = (
            connection.execute(sa.text("SELECT DISTINCT day FROM monitoring_metrics"))
            .scalars()
            .all()
        )
    assert left == [new] and old not in left
    assert days == [TODAY]
