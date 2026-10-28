"""The training smoke test: `generate -> features -> train -> evaluate` through the CLI.

On a tiny market and short period, so CI runs it as its own fast job (`pytest -m smoke`) and
notices a broken pipeline before the slow suite finishes. It checks that the stages hand each
other their artifacts and that the result is sane, not how good the model is.
"""

import json
from pathlib import Path

import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG

from txrisk.cli import main
from txrisk.models.bundle import ModelBundle

pytestmark = pytest.mark.smoke

REPO = Path(__file__).resolve().parents[1]

# The tiny market: 21 days, about 600 Wallets, and enough fraud to train and judge on.
MARKET_EDITS = {
    "days = 120": "days = 21",
    "wallets = 10000": "wallets = 600",
    "agents = 300": "agents = 30",
    "merchants = 200": "merchants = 20",
    "rate = 0.002": "rate = 0.03",
}
EVALUATION = (
    "label_maturity_days = 3\nalert_budget_per_day = 20\nwallet_age_band_edges = [30, 180]\n"
    "[splits]\ntrain = [0, 8]\ncalibration = [8, 11]\nthresholds = [11, 14]\n"
    "evaluation = [14, 21]\n"
)


def test_a_tiny_market_goes_from_generation_to_an_evaluated_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("TXRISK_PSEUDONYM_KEY", "smoke-test-key")
    market = DEFAULT_CONFIG.read_text(encoding="utf-8")
    for old, new in MARKET_EDITS.items():
        assert old in market, f"config/generator.toml no longer says {old!r}; update the smoke test"
        market = market.replace(old, new)
    (tmp_path / "generator.toml").write_text(market, encoding="utf-8")
    (tmp_path / "evaluation.toml").write_text(EVALUATION, encoding="utf-8")
    shared = [
        "--config", str(tmp_path / "generator.toml"),
        "--feature-config", str(REPO / "config" / "features.toml"),
    ]  # fmt: skip
    data, features = tmp_path / "data", tmp_path / "features.parquet"
    evaluation = ["--evaluation-config", str(tmp_path / "evaluation.toml")]
    rules = ["--rules-config", str(REPO / "config" / "rules.toml")]

    assert main(["generate", "--config", shared[1], "--seed", "7", "--out", str(data)]) == 0
    assert main(["features", "--data", str(data), *shared, "--out", str(features)]) == 0
    assert (
        main(
            [
                "train", "--data", str(data), "--features", str(features), *shared,
                *evaluation, *rules, "--no-mlflow", "--out", str(tmp_path / "models"),
            ]
        )
        == 0
    )  # fmt: skip
    (folder,) = (tmp_path / "models").iterdir()
    assert (
        main(
            [
                "evaluate", "--data", str(data), "--features", str(features), *shared,
                *evaluation, *rules, "--scorer", "stacked", "--out", str(tmp_path / "report"),
            ]
        )
        == 0
    )  # fmt: skip

    bundle = ModelBundle.load(folder)
    metrics = json.loads((tmp_path / "report" / "metrics.json").read_text(encoding="utf-8"))
    scores = pd.read_parquet(tmp_path / "report" / "scores.parquet")
    assert bundle.thresholds.high > bundle.thresholds.medium
    assert metrics["scorer"] == "stacked"
    # Better than chance, on a market this small; the real bars are set by the evaluation tests.
    assert metrics["ranking"]["roc_auc"] > 0.6
    assert scores["risk_score"].between(0, 100).all()
    assert len(scores) == metrics["transactions"] > 0
