"""`txrisk evaluate --scorer anomaly` and `--scorer lof`, end to end on a small market."""

import json
from pathlib import Path

import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG, KEY

from txrisk.cli import main
from txrisk.evaluation import load_evaluation_config
from txrisk.evaluation.scorers import anomaly_scores
from txrisk.features import load_feature_config
from txrisk.generator import GeneratorConfig, generate, write_dataset

REPO = Path(__file__).resolve().parents[1]
KYC_LIMITS = {"minimum": 1000.0, "medium": 5000.0, "enhanced": 15000.0}


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("anomaly")
    config = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 600, "agents": 30, "merchants": 20}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )
    write_dataset(generate(config, seed=4, key=KEY), root / "data")
    (root / "generator.toml").write_text(
        DEFAULT_CONFIG.read_text(encoding="utf-8").replace("days = 120", "days = 21"),
        encoding="utf-8",
    )
    (root / "evaluation.toml").write_text(
        "label_maturity_days = 3\nalert_budget_per_day = 20\nwallet_age_band_edges = [30, 180]\n"
        "[splits]\ntrain = [0, 8]\ncalibration = [8, 11]\nthresholds = [11, 14]\n"
        "evaluation = [14, 21]\n",
        encoding="utf-8",
    )
    return root


def _evaluate(market: Path, scorer: str, *extra: str) -> dict:
    out = market / scorer
    exit_code = main(
        [
            "evaluate",
            *extra,
            "--scorer", scorer,
            "--data", str(market / "data"),
            "--config", str(market / "generator.toml"),
            "--evaluation-config", str(market / "evaluation.toml"),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--rules-config", str(REPO / "config" / "rules.toml"),
            "--out", str(out),
        ]
    )  # fmt: skip
    assert exit_code == 0
    return json.loads((out / "metrics.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("scorer", ["anomaly", "lof"])
def test_an_anomaly_scorer_is_judged_like_any_other(market: Path, scorer: str):
    metrics = _evaluate(market, scorer)
    scores = pd.read_parquet(market / scorer / "scores.parquet")

    assert metrics["scorer"] == scorer
    assert 0 <= metrics["ranking"]["pr_auc"] <= 1
    assert scores["anomaly_score"].between(0, 1).all()
    assert scores["risk_score"].equals(scores["anomaly_score"] * 100)
    assert metrics["at_high"]["flagged"] == (scores["risk_band"] == "High").sum()


def test_high_is_set_to_the_alert_budget_on_the_threshold_days(market: Path):
    metrics = _evaluate(market, "anomaly")

    # Set to 20 a day on days 11-14; the evaluation days land near it, not exactly on it.
    assert 10 <= metrics["at_high"]["per_day"] <= 40


def test_transactions_outside_every_split_are_left_out(market: Path):
    # The splits stop at day 20, but the data runs to day 21, as when a Fraud scenario that
    # starts near the end of the period runs past it.
    short = market / "short.toml"
    short.write_text(
        (market / "evaluation.toml").read_text(encoding="utf-8").replace("[14, 21]", "[14, 20]"),
        encoding="utf-8",
    )
    transactions = pd.read_parquet(market / "data" / "transactions.parquet")

    scores = anomaly_scores(
        transactions,
        pd.read_parquet(market / "data" / "wallets.parquet"),
        pd.read_parquet(market / "data" / "sim_swaps.parquet"),
        features=load_feature_config(REPO / "config" / "features.toml", KYC_LIMITS),
        evaluation=load_evaluation_config(short),
        period_start=pd.Timestamp("2026-01-01").date(),
    )

    last_day = pd.Timestamp("2026-01-21", tz="UTC")
    evaluated = transactions.set_index("transaction_id").loc[
        scores["transaction_id"], "occurred_at"
    ]
    assert (evaluated < last_day).all()
    assert (
        len(scores)
        == (
            (transactions["occurred_at"] >= pd.Timestamp("2026-01-15", tz="UTC"))
            & (transactions["occurred_at"] < last_day)
        ).sum()
    )


def test_a_saved_feature_table_gives_the_same_report(market: Path):
    built = _evaluate(market, "anomaly")
    table = market / "features.parquet"
    assert main(
        [
            "features",
            "--data", str(market / "data"),
            "--config", str(market / "generator.toml"),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--out", str(table),
        ]
    ) == 0  # fmt: skip

    reused = _evaluate(market, "anomaly", "--features", str(table))

    assert reused == built
