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
    exit_code = main(
        [
            "evaluate",
            "--data", str(root / "data"),
            "--config", str(market),
            "--evaluation-config", str(evaluation),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--rules-config", str(REPO / "config" / "rules.toml"),
            "--out", str(root / "report"),
        ]
    )  # fmt: skip
    assert exit_code == 0
    return root


def _metrics(run: Path) -> dict:
    return json.loads((run / "report" / "metrics.json").read_text(encoding="utf-8"))


def _evaluation_days(run: Path) -> pd.DataFrame:
    tx = pd.read_parquet(run / "data" / "transactions.parquet")
    start = pd.Timestamp("2026-01-01", tz="UTC")
    return tx[tx["occurred_at"] >= start + pd.Timedelta(days=14)]


def test_the_report_has_metrics_curves_and_scores(run: Path):
    report = run / "report"

    assert {p.name for p in report.iterdir()} == {
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
