"""`txrisk evaluate --scorer anomaly` and `--scorer lof`, end to end on a small market."""

import json
from pathlib import Path

import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG, KEY

from txrisk.cli import main
from txrisk.generator import GeneratorConfig, generate, write_dataset

REPO = Path(__file__).resolve().parents[1]


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


def _evaluate(market: Path, scorer: str) -> dict:
    out = market / scorer
    exit_code = main(
        [
            "evaluate",
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
