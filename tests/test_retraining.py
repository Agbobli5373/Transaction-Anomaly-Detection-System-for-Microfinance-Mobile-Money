"""Retraining: a bundle trained from what Postgres holds at the simulated time, registered as the
Challenger model; Fraud labels seeded for the days before the live period; early retraining on a
drift action alert."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import mlflow
import pandas as pd
import pytest
import sqlalchemy as sa
from conftest import DEFAULT_CONFIG
from market_helpers import CONFIG, RULES, START, Market, build_market, train
from mlflow import MlflowClient

from txrisk.cli import main
from txrisk.evaluation import load_evaluation_config
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.models.bundle import ModelBundle
from txrisk.models.registry import MODEL_NAME
from txrisk.monitoring import load_monitoring_config
from txrisk.retraining import (
    NotEnoughHistory,
    drift_finding,
    load_retraining_config,
    load_training_set,
    retrain,
    shifted_evaluation,
    training_cutoff,
)
from txrisk.scheduler import load_scheduler_config
from txrisk.store import (
    FraudLabels,
    SimulationClock,
    challenger_version,
    create_engine,
    seed,
)

ROOT = Path(__file__).resolve().parents[1]
DAY = timedelta(days=1)
LIVE_DAY = 14  # the day the evaluation (live) period starts, in the market's own config
START_AT = datetime(START.year, START.month, START.day, tzinfo=UTC)
MONITORING = load_monitoring_config(ROOT / "config" / "monitoring.toml")
RETRAINING = load_retraining_config(ROOT / "config" / "retraining.toml")


def day_start(day: int) -> datetime:
    return START_AT + day * DAY


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The market's dataset on disk, without Ground truth and Benign anomalies: nothing here may
    need them."""
    folder = tmp_path_factory.mktemp("retraining-data")
    write_dataset(market.dataset, folder)
    for name in ("ground_truth", "benign_anomalies"):
        (folder / f"{name}.parquet").unlink()
    return folder


@pytest.fixture(scope="module")
def configs(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The market's 21-day generator and evaluation configs, as `txrisk train` is tested with."""
    folder = tmp_path_factory.mktemp("retraining-config")
    (folder / "generator.toml").write_text(
        DEFAULT_CONFIG.read_text(encoding="utf-8").replace("days = 120", "days = 21"),
        encoding="utf-8",
    )
    (folder / "evaluation.toml").write_text(
        "label_maturity_days = 3\nalert_budget_per_day = 20\nwallet_age_band_edges = [30, 180]\n"
        "[splits]\ntrain = [0, 8]\ncalibration = [8, 11]\nthresholds = [11, 14]\n"
        "evaluation = [14, 21]\n",
        encoding="utf-8",
    )
    return folder


def seeded(database: str, data: Path, until_day: int) -> sa.Engine:
    """The database as it is when the live period starts on `until_day`: the history and the
    Fraud labels before it, and the simulation clock a few hours into that day."""
    engine = create_engine(database)
    seed(engine, data, until=day_start(until_day))
    SimulationClock(engine).advance(day_start(until_day) + timedelta(hours=3))
    return engine


def retrain_at(engine: sa.Engine, market: Market, models: Path, **options: object):
    settings: dict[str, object] = {
        "trigger": "weekly",
        "reason": "test",
        "clock": SimulationClock(engine).now(),
        "models": models,
        "rules": RULES,
        "features": market.feature_config,
        "evaluation": CONFIG,
        "period_start": START,
        "seed": 11,
        "baseline_bins": 10,
    }
    return retrain(engine, **{**settings, **options})  # type: ignore[arg-type]


def rows(engine: sa.Engine, query: str) -> list[sa.Row]:
    with engine.connect() as connection:
        return list(connection.execute(sa.text(query)))


# --- seeding Fraud labels ----------------------------------------------------------------------


def test_seeding_stores_the_fraud_labels_discovered_before_the_cutoff(
    market: Market, data: Path, database: str
):
    engine = create_engine(database)
    cutoff = day_start(LIVE_DAY)

    counts = seed(engine, data, until=cutoff)

    discoveries = market.discoveries
    before = discoveries[discoveries["discovered_at"] < cutoff]
    assert 0 < len(before) < len(discoveries)  # some are discovered before, some after
    assert counts["fraud_labels"] == len(before)
    stored = FraudLabels(engine).known_at(cutoff + 100 * DAY)  # whatever is stored, at any time
    assert {label.transaction_id for label in stored} == set(before["transaction_id"])
    assert {label.source for label in stored} == set(before["source"])


def test_seeded_labels_keep_their_own_discovery_time_so_none_is_visible_early(
    market: Market, data: Path, database: str
):
    engine = create_engine(database)
    seed(engine, data, until=day_start(LIVE_DAY))
    labels = FraudLabels(engine)

    known = {label.transaction_id: label.discovered_at for label in labels.known_at(day_start(10))}

    discoveries = market.discoveries.set_index("transaction_id")["discovered_at"]
    assert 0 < len(known) < len(labels.known_at(day_start(LIVE_DAY)))
    assert all(found < day_start(10) for found in known.values())
    for transaction_id, found in known.items():
        assert found == discoveries[transaction_id].floor("us").to_pydatetime()


def test_a_discovery_after_the_cutoff_is_not_seeded(market: Market, data: Path, database: str):
    """It arrives as a `fraud.discovered` event when the replay reaches its event time."""
    engine = create_engine(database)
    cutoff = day_start(LIVE_DAY)
    late = market.discoveries[market.discoveries["discovered_at"] >= cutoff]
    assert len(late) > 0

    seed(engine, data, until=cutoff)

    stored = {label.transaction_id for label in FraudLabels(engine).known_at(cutoff + 100 * DAY)}
    assert not stored & set(late["transaction_id"])


def test_seeding_labels_again_adds_only_the_missing_ones(market: Market, data: Path, database: str):
    engine = create_engine(database)
    seed(engine, data, until=day_start(10))

    counts = seed(engine, data, until=day_start(LIVE_DAY))

    discoveries = market.discoveries["discovered_at"]
    assert (
        counts["fraud_labels"]
        == ((discoveries >= day_start(10)) & (discoveries < day_start(LIVE_DAY))).sum()
    )


def test_seeding_never_needs_ground_truth_or_benign_anomalies(data: Path, database: str):
    assert not (data / "ground_truth.parquet").exists()
    assert not (data / "benign_anomalies.parquet").exists()

    counts = seed(create_engine(database), data, until=day_start(LIVE_DAY))

    assert counts["fraud_labels"] > 0


# --- the training set --------------------------------------------------------------------------


def test_the_training_set_is_what_postgres_holds_before_the_cutoff(
    market: Market, data: Path, database: str
):
    engine = seeded(database, data, LIVE_DAY)
    cutoff = day_start(LIVE_DAY)
    # A live discovery after the cutoff, and a Transaction after it, both stored since.
    FraudLabels(engine).record("T-not-yet", cutoff + timedelta(hours=1), "complaint")
    seed(engine, data, until=cutoff + 2 * DAY)

    found = load_training_set(engine, cutoff)

    transactions = market.transactions
    assert len(found.transactions) == (transactions["occurred_at"] < cutoff).sum()
    assert (found.transactions["occurred_at"] < cutoff).all()
    assert (found.sim_swaps["occurred_at"] < cutoff).all()
    assert (found.wallets["registered_at"] < cutoff).all()
    assert (found.discoveries["discovered_at"] < cutoff).all()
    assert "T-not-yet" not in set(found.discoveries["transaction_id"])
    assert len(found.discoveries) == (market.discoveries["discovered_at"] < cutoff).sum()
    # Fraud labels carry only what a Fraud discovery carries: nothing of Ground truth.
    assert list(found.discoveries.columns) == ["transaction_id", "discovered_at", "source"]
    assert not {"scenario", "instance_id", "is_fraud"} & set(found.transactions.columns)


def test_the_training_set_matches_the_offline_tables_the_first_champion_trained_on(
    market: Market, data: Path, database: str
):
    engine = seeded(database, data, LIVE_DAY)
    cutoff = day_start(LIVE_DAY)

    found = load_training_set(engine, cutoff).transactions

    offline = market.transactions[market.transactions["occurred_at"] < cutoff]
    offline = offline.sort_values(["occurred_at", "transaction_id"]).reset_index(drop=True)
    # Postgres keeps microseconds, as seeding documents.
    offline["occurred_at"] = (
        offline["occurred_at"].dt.floor("us").astype(found["occurred_at"].dtype)
    )
    pd.testing.assert_frame_equal(
        found[list(offline.columns)].reset_index(drop=True),
        offline,
        check_dtype=False,
        check_exact=True,
    )


# --- the splits slide with the cutoff ----------------------------------------------------------


def test_retraining_at_the_day_the_champion_went_live_uses_the_champions_splits():
    shifted = shifted_evaluation(CONFIG, day_start(14), START)

    assert shifted.splits == CONFIG.splits


def test_later_retraining_slides_the_label_and_threshold_windows_forward():
    shifted = shifted_evaluation(CONFIG, day_start(17), START)

    splits = shifted.splits
    assert (splits.train.start, splits.train.end) == (0, 11)
    assert (splits.calibration.start, splits.calibration.end) == (11, 14)
    assert (splits.thresholds.start, splits.thresholds.end) == (14, 17)
    assert (splits.evaluation.start, splits.evaluation.end) == (17, 24)
    assert shifted.label_maturity_days == CONFIG.label_maturity_days


def test_the_shipped_config_slides_from_day_90_to_day_97():
    shipped = load_evaluation_config(ROOT / "config" / "evaluation.toml")

    splits = shifted_evaluation(shipped, day_start(97), date(2026, 1, 1)).splits

    assert (splits.train.end, splits.calibration.end, splits.thresholds.end) == (52, 67, 97)


def test_retraining_too_early_to_fit_the_splits_is_refused():
    with pytest.raises(NotEnoughHistory, match="too early"):
        shifted_evaluation(CONFIG, day_start(6), START)


def test_the_training_cutoff_is_the_start_of_the_simulated_day():
    assert training_cutoff(datetime(2026, 1, 20, 17, 45, tzinfo=UTC)) == day_start(19)


# --- retraining --------------------------------------------------------------------------------


def test_retraining_learns_from_the_fraud_labels_seeded_for_the_days_before_the_live_period(
    market: Market, data: Path, database: str, tmp_path: Path
):
    engine = seeded(database, data, LIVE_DAY)

    done = retrain_at(engine, market, tmp_path)

    bundle = ModelBundle.load(done.folder)
    assert (
        done.fraud_labels == (market.discoveries["discovered_at"] < day_start(LIVE_DAY)).sum() > 0
    )
    assert bundle.version == done.bundle_version == done.folder.name
    # The stacker and the supervised model saw discovered fraud: with no labels they see none.
    assert bundle.metrics["stacker_fraud"] > 0
    assert bundle.fitted_on["labels_known_on_day"] == LIVE_DAY


def test_retraining_from_postgres_reproduces_the_offline_training_at_the_same_cutoff(
    market: Market, data: Path, database: str, tmp_path: Path
):
    """The same bundle, to the version hash, as training on the Parquet tables: nothing in the
    database round trip changes what the models learn."""
    engine = seeded(database, data, LIVE_DAY)

    done = retrain_at(engine, market, tmp_path)

    assert done.bundle_version == train(market).version


def test_retraining_makes_the_new_bundle_the_challenger_and_records_the_run(
    market: Market, data: Path, database: str, tmp_path: Path
):
    engine = seeded(database, data, LIVE_DAY)
    assert challenger_version(engine) is None

    done = retrain_at(engine, market, tmp_path, trigger="drift", reason="PSI of x was 0.3")

    assert challenger_version(engine) == done.bundle_version
    (run,) = rows(engine, "SELECT * FROM retraining_runs")
    assert (run.trigger, run.reason) == ("drift", "PSI of x was 0.3")
    assert run.cutoff == day_start(LIVE_DAY)
    assert run.bundle_version == done.bundle_version
    assert run.fraud_labels == done.fraud_labels
    assert run.transactions == done.transactions
    (pointer,) = rows(engine, "SELECT run_id FROM challenger_model")
    assert pointer.run_id == run.run_id


def test_retraining_reads_nothing_after_the_simulated_time(
    market: Market, data: Path, database: str, tmp_path: Path
):
    """Data stored beyond the cutoff (a later day seeded, a later discovery) changes nothing."""
    engine = seeded(database, data, LIVE_DAY)
    first = retrain_at(engine, market, tmp_path)

    seed(engine, data, until=day_start(LIVE_DAY + 4))  # later Transactions, labels and SIM swaps
    FraudLabels(engine).record("T-later", day_start(LIVE_DAY) + timedelta(hours=1), "case")
    SimulationClock(engine).advance(day_start(LIVE_DAY) + timedelta(hours=5))  # still the same day
    second = retrain_at(engine, market, tmp_path / "again")

    assert second.bundle_version == first.bundle_version
    assert second.transactions == first.transactions
    assert second.fraud_labels == first.fraud_labels


def test_a_later_retraining_uses_the_labels_discovered_in_the_meantime(
    market: Market, data: Path, database: str, tmp_path: Path
):
    engine = seeded(database, data, LIVE_DAY)
    first = retrain_at(engine, market, tmp_path)

    # The live period goes on: a week later more Transactions and discoveries are stored.
    seed(engine, data, until=day_start(LIVE_DAY + 3))
    SimulationClock(engine).advance(day_start(LIVE_DAY + 3) + timedelta(hours=2))
    second = retrain_at(engine, market, tmp_path)

    assert second.cutoff == day_start(LIVE_DAY + 3)
    assert second.transactions > first.transactions
    assert second.fraud_labels > first.fraud_labels
    assert second.bundle_version != first.bundle_version
    assert challenger_version(engine) == second.bundle_version
    assert [
        r.bundle_version for r in rows(engine, "SELECT * FROM retraining_runs ORDER BY run_id")
    ] == [
        first.bundle_version,
        second.bundle_version,
    ]


def test_retraining_logs_the_bundle_to_mlflow_and_gives_the_new_version_the_challenger_alias(
    market: Market, data: Path, database: str, tmp_path: Path
):
    engine = seeded(database, data, LIVE_DAY)
    uri = f"sqlite:///{tmp_path / 'mlflow.db'}"

    first = retrain_at(
        engine, market, tmp_path / "models", mlflow_uri=uri, mlflow_artifacts=tmp_path / "mlruns"
    )
    SimulationClock(engine).advance(day_start(LIVE_DAY + 3))
    seed(engine, data, until=day_start(LIVE_DAY + 3))
    second = retrain_at(
        engine, market, tmp_path / "models", mlflow_uri=uri, mlflow_artifacts=tmp_path / "mlruns"
    )

    mlflow.set_tracking_uri(uri)
    client = MlflowClient()
    assert (first.registry_version, second.registry_version) == ("1", "2")
    assert client.get_model_version(MODEL_NAME, "2").tags["bundle_version"] == second.bundle_version
    aliased = client.get_model_version_by_alias(MODEL_NAME, "challenger")
    assert str(aliased.version) == "2"
    run = client.get_run(aliased.run_id)
    assert run.data.params["trigger"] == "weekly"
    assert run.data.params["cutoff"] == day_start(LIVE_DAY + 3).isoformat()
    assert run.data.metrics["threshold_high"] > 0


def test_a_retraining_run_while_another_is_running_does_nothing(
    market: Market, data: Path, database: str, tmp_path: Path
):
    from txrisk.retraining import AlreadyRunning
    from txrisk.retraining.job import _LOCK

    engine = seeded(database, data, LIVE_DAY)
    with engine.connect() as holder:
        holder.execute(sa.select(sa.func.pg_advisory_lock(_LOCK)))
        with pytest.raises(AlreadyRunning):
            retrain_at(engine, market, tmp_path)
        holder.execute(sa.select(sa.func.pg_advisory_unlock(_LOCK)))

    assert challenger_version(engine) is None
    assert retrain_at(engine, market, tmp_path).bundle_version  # the lock was released


# --- early retraining on a drift action alert --------------------------------------------------


def store_psi(
    engine: sa.Engine, day: date, value: float, *, metric: str = "psi", member: str = "amount"
) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO monitoring_metrics (day, metric, member, value) "
                "VALUES (:day, :metric, :member, :value)"
            ),
            {"day": day, "metric": metric, "member": member, "value": value},
        )


def test_a_psi_at_the_action_threshold_asks_for_retraining(database: str):
    engine = create_engine(database)
    store_psi(engine, date(2026, 1, 20), MONITORING.psi.action)

    found = drift_finding(engine, clock=day_start(20), monitoring=MONITORING, config=RETRAINING)

    assert found is not None
    assert "PSI of amount overall" in found and "2026-01-20" in found


def test_the_psi_among_the_alerts_asks_for_retraining_too(database: str):
    engine = create_engine(database)
    store_psi(
        engine, date(2026, 1, 20), 0.31, metric="psi_alerted", member="seconds_since_sim_swap"
    )

    found = drift_finding(engine, clock=day_start(21), monitoring=MONITORING, config=RETRAINING)

    assert found is not None and "seconds_since_sim_swap among the Alerts" in found


def test_a_warning_is_not_an_action_alert(database: str):
    engine = create_engine(database)
    store_psi(engine, date(2026, 1, 20), MONITORING.psi.action - 0.01)
    store_psi(engine, date(2026, 1, 20), 5.0, metric="label_drop_recall")  # not a PSI

    assert (
        drift_finding(engine, clock=day_start(21), monitoring=MONITORING, config=RETRAINING) is None
    )


def test_drift_seen_before_the_last_retraining_is_not_new_and_it_waits_between_runs(
    market: Market, data: Path, database: str, tmp_path: Path
):
    engine = seeded(database, data, LIVE_DAY)
    retrain_at(engine, market, tmp_path)  # cutoff: day 14
    store_psi(engine, date(2026, 1, 14) - DAY, 0.9)  # the day before: the run has seen it

    def finding(at_day: int) -> str | None:
        return drift_finding(
            engine, clock=day_start(at_day), monitoring=MONITORING, config=RETRAINING
        )

    assert finding(LIVE_DAY + 4) is None
    store_psi(engine, date(2026, 1, 15), 0.4)  # drift on a day after the cutoff
    assert finding(LIVE_DAY + 1) is None  # but the last run was too recent
    assert finding(LIVE_DAY + RETRAINING.drift.min_days_between_runs) is not None


def retrain_options(configs: Path, data: Path, database: str, models: Path) -> list[str]:
    return [
        "retrain",
        "--database-url", database,
        "--config", str(configs / "generator.toml"),
        "--evaluation-config", str(configs / "evaluation.toml"),
        "--feature-config", str(ROOT / "config" / "features.toml"),
        "--rules-config", str(ROOT / "config" / "rules.toml"),
        "--models", str(models),
        "--seed", "11",
        "--no-mlflow",
    ]  # fmt: skip


def test_a_drift_action_alert_retrains_before_the_weekly_schedule(
    data: Path, configs: Path, database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    engine = seeded(database, data, LIVE_DAY)
    options = [*retrain_options(configs, data, database, tmp_path), "--if-drift"]

    assert main(options) == 0
    assert "no drift action alert" in capsys.readouterr().out
    assert challenger_version(engine) is None

    store_psi(engine, date(2026, 1, 14), 0.5, metric="psi_alerted", member="seconds_since_sim_swap")
    assert main(options) == 0

    out = capsys.readouterr().out
    assert "retrained at 2026-01-15" in out or "retrained at 2026-01-14" in out
    (run,) = rows(engine, "SELECT trigger, reason FROM retraining_runs")
    assert run.trigger == "drift"
    assert "seconds_since_sim_swap" in run.reason
    assert challenger_version(engine) is not None


def test_the_weekly_job_retrains_whether_or_not_there_is_drift(
    data: Path, configs: Path, database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    engine = seeded(database, data, LIVE_DAY)

    assert main(retrain_options(configs, data, database, tmp_path)) == 0

    assert "Challenger model risk-" in capsys.readouterr().out
    (run,) = rows(engine, "SELECT trigger FROM retraining_runs")
    assert run.trigger == "weekly"
    assert ModelBundle.load(tmp_path / str(challenger_version(engine))).version


def test_retraining_before_the_clock_has_started_does_nothing(
    configs: Path, database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], data: Path
):
    assert main(retrain_options(configs, data, database, tmp_path)) == 0

    assert "clock has not started" in capsys.readouterr().out


def test_retraining_as_the_application_role_is_refused(
    configs: Path, database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], data: Path
):
    from api_helpers import as_app_role

    options = retrain_options(configs, data, as_app_role(database), tmp_path)

    assert main(options) == 2
    assert "run it as the owner" in capsys.readouterr().err


def test_the_scheduler_fires_retraining_weekly_and_the_drift_check_daily():
    jobs = {j.name: j for j in load_scheduler_config(ROOT / "config" / "scheduler.toml").jobs}

    assert (jobs["retrain-weekly"].schedule.value, jobs["retrain-weekly"].command) == (
        "weekly",
        ("txrisk", "retrain"),
    )
    assert (jobs["retrain-on-drift"].schedule.value, jobs["retrain-on-drift"].command) == (
        "daily",
        ("txrisk", "retrain", "--if-drift"),
    )
