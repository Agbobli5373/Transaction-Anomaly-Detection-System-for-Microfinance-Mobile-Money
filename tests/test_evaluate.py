"""`txrisk evaluate`: score the evaluation days and report on them, end to end."""

import json
from pathlib import Path

import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG, KEY

from txrisk.cli import main
from txrisk.generator import GeneratorConfig, generate, write_dataset

REPO = Path(__file__).resolve().parents[1]
SEGMENTS = {"region", "kyc_tier", "channel", "wallet_age_band"}


@pytest.fixture(scope="module")
def run(small_config: GeneratorConfig, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 21-day market with plenty of fraud, evaluated on its last week."""
    root = tmp_path_factory.mktemp("evaluate")
    config = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 800, "agents": 40, "merchants": 20}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )
    write_dataset(generate(config, seed=4, key=KEY), root / "data")
    market = root / "generator.toml"
    market.write_text(
        DEFAULT_CONFIG.read_text(encoding="utf-8").replace("days = 120", "days = 21"),
        encoding="utf-8",
    )
    evaluation = root / "evaluation.toml"
    evaluation.write_text(
        "label_maturity_days = 3\nalert_budget_per_day = 20\nwallet_age_band_edges = [30, 180]\n"
        "[splits]\ntrain = [0, 8]\ncalibration = [8, 11]\nthresholds = [11, 14]\n"
        "evaluation = [14, 21]\n",
        encoding="utf-8",
    )
    _evaluate(root, "--out", str(root / "report"))
    return root


def _evaluate(root: Path, *options: str) -> None:
    exit_code = main(
        [
            "evaluate",
            "--data", str(root / "data"),
            "--config", str(root / "generator.toml"),
            "--evaluation-config", str(root / "evaluation.toml"),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--rules-config", str(REPO / "config" / "rules.toml"),
            *options,
        ]
    )  # fmt: skip
    assert exit_code == 0


@pytest.fixture(scope="module")
def supervised(run: Path) -> Path:
    """The same market judged by the supervised fraud probability alone."""
    _evaluate(run, "--scorer", "supervised", "--out", str(run / "supervised"))
    return run / "supervised"


def _metrics(run: Path) -> dict:
    return json.loads((run / "report" / "metrics.json").read_text(encoding="utf-8"))


def _evaluation_days(run: Path) -> pd.DataFrame:
    tx = pd.read_parquet(run / "data" / "transactions.parquet")
    start = pd.Timestamp("2026-01-01", tz="UTC")
    return tx[tx["occurred_at"] >= start + pd.Timedelta(days=14)]


def test_the_report_has_metrics_curves_and_scores(run: Path):
    report = run / "report"

    assert {p.name for p in report.iterdir()} == {
        "daily_high.png",
        "metrics.json",
        "pr_curve.png",
        "roc_curve.png",
        "scores.parquet",
    }


def test_the_report_covers_exactly_the_evaluation_days(run: Path):
    metrics = _metrics(run)
    evaluated = _evaluation_days(run)
    truth = pd.read_parquet(run / "data" / "ground_truth.parquet")

    assert metrics["scorer"] == "rules-only"
    assert metrics["days"] == [14, 21]
    assert metrics["transactions"] == len(evaluated)
    assert metrics["fraud"] == evaluated["transaction_id"].isin(truth["transaction_id"]).sum()


def test_the_report_counts_high_alerts_from_the_scores_it_wrote(run: Path):
    metrics = _metrics(run)
    scores = pd.read_parquet(run / "report" / "scores.parquet")

    assert metrics["at_high"]["flagged"] == (scores["risk_band"] == "High").sum()
    assert metrics["at_high"]["per_day"] == pytest.approx(metrics["at_high"]["flagged"] / 7)


def test_the_report_counts_the_high_alerts_of_each_evaluation_day(run: Path):
    metrics = _metrics(run)
    scores = pd.read_parquet(run / "report" / "scores.parquet")
    evaluated = _evaluation_days(run).merge(scores, on="transaction_id")

    daily = metrics["high_per_day"]

    assert list(daily) == [f"2026-01-{day}" for day in range(15, 22)]
    for day, flagged in evaluated.groupby(evaluated["occurred_at"].dt.strftime("%Y-%m-%d")):
        assert daily[day] == (flagged["risk_band"] == "High").sum()


def test_the_report_measures_what_fraud_operations_care_about(run: Path):
    metrics = _metrics(run)
    evaluated = _evaluation_days(run)
    truth = pd.read_parquet(run / "data" / "ground_truth.parquet")
    scenarios = set(
        truth.loc[truth["transaction_id"].isin(evaluated["transaction_id"]), "scenario"]
    )

    assert 0 <= metrics["ranking"]["roc_auc"] <= 1
    assert 0 <= metrics["ranking"]["pr_auc"] <= 1
    assert metrics["at_alert_budget"]["alerts"] == 20 * 7
    assert set(metrics["recall_by_scenario"]) == scenarios
    assert set(metrics["recall_against"]) == {"ground_truth", "discovered_labels"}
    assert metrics["recall_against"]["ground_truth"] == metrics["at_high"]["recall"]
    assert set(metrics["segments"]) == SEGMENTS
    for segment in metrics["segments"].values():
        assert sum(group["transactions"] for group in segment.values()) == len(evaluated)


def test_the_report_has_the_pr_and_roc_curves_as_points_for_pages_to_draw(run: Path):
    curves = _metrics(run)["curves"]

    assert set(curves) == {"pr", "roc"}
    for curve in curves.values():
        assert 2 <= len(curve["x"]) == len(curve["y"]) <= 200
    assert curves["roc"]["x"][0] == 0 and curves["roc"]["x"][-1] == 1


def test_a_saved_bundle_is_judged_and_its_report_is_saved_in_its_own_folder(run: Path):
    models = run / "models"
    exit_code = main(
        [
            "train",
            "--data", str(run / "data"),
            "--config", str(run / "generator.toml"),
            "--evaluation-config", str(run / "evaluation.toml"),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--rules-config", str(REPO / "config" / "rules.toml"),
            "--out", str(models),
            "--no-mlflow",
        ]
    )  # fmt: skip
    assert exit_code == 0
    (bundle,) = models.iterdir()

    _evaluate(run, "--scorer", "bundle", "--bundle", str(bundle))

    metrics = json.loads((bundle / "evaluation" / "metrics.json").read_text(encoding="utf-8"))
    scores = pd.read_parquet(bundle / "evaluation" / "scores.parquet")
    assert metrics["scorer"] == "bundle"
    assert set(scores["model_version"]) == {bundle.name}
    assert metrics["transactions"] == len(_evaluation_days(run))
    assert {"ranking", "recall_by_scenario", "recall_against", "segments", "curves"} <= set(metrics)


def test_judging_a_bundle_needs_the_bundle(run: Path, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(AssertionError):
        _evaluate(run, "--scorer", "bundle")

    assert "--bundle" in capsys.readouterr().err


def test_the_supervised_risk_score_is_the_fraud_probability_banded_at_the_alert_budget(
    supervised: Path,
):
    metrics = json.loads((supervised / "metrics.json").read_text(encoding="utf-8"))
    scores = pd.read_parquet(supervised / "scores.parquet")

    assert metrics["scorer"] == "supervised"
    assert len(scores) == metrics["transactions"]
    assert scores["fraud_probability"].between(0, 1).all()
    assert scores["risk_score"].equals(scores["fraud_probability"] * 100)
    # High is set to the Alert budget (20 a day) on days 11-14, like every scorer's.
    assert 10 <= metrics["at_high"]["per_day"] <= 40
    high = scores.loc[scores["risk_band"] == "High", "risk_score"]
    assert high.min() > scores.loc[scores["risk_band"] != "High", "risk_score"].max()


def test_the_probability_gets_a_reliability_check_before_and_after_calibration(supervised: Path):
    metrics = json.loads((supervised / "metrics.json").read_text(encoding="utf-8"))
    reliability = metrics["reliability"]

    # Calibrated on Fraud labels, the probability is the chance of fraud that gets discovered,
    # so it is judged against eventual Fraud discovery as well as against Ground truth.
    assert set(reliability) == {"eventually_discovered", "ground_truth"}
    for against in reliability.values():
        assert set(against) == {"raw", "calibrated"}
        for check in against.values():
            assert 0 <= check["brier_score"] <= 1
            assert 0 <= check["expected_calibration_error"] <= 1
            assert sum(b["transactions"] for b in check["bins"]) == metrics["transactions"]


def test_only_a_probability_gets_a_reliability_check(run: Path):
    assert "reliability" not in _metrics(run)
