"""`txrisk train` and `txrisk evaluate --scorer stacked`, end to end on a small market."""

import json
from pathlib import Path

import mlflow
import pandas as pd
import pytest
from conftest import DEFAULT_CONFIG, KEY
from mlflow import MlflowClient

from txrisk.cli import main
from txrisk.generator import GeneratorConfig, generate, write_dataset
from txrisk.models.bundle import ModelBundle
from txrisk.models.registry import MODEL_NAME

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig, tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("train")
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


def _options(market: Path) -> list[str]:
    return [
        "--data", str(market / "data"),
        "--config", str(market / "generator.toml"),
        "--evaluation-config", str(market / "evaluation.toml"),
        "--feature-config", str(REPO / "config" / "features.toml"),
        "--rules-config", str(REPO / "config" / "rules.toml"),
    ]  # fmt: skip


def _train(market: Path, store: Path, models: Path) -> None:
    """Train, writing the bundle under `models` and tracking the run in MLflow under `store`."""
    exit_code = main(
        [
            "train",
            *_options(market),
            "--out", str(models),
            "--mlflow-uri", f"sqlite:///{store / 'mlflow.db'}",
            "--mlflow-artifacts", str(store / "mlruns"),
        ]
    )  # fmt: skip
    assert exit_code == 0


@pytest.fixture(scope="module")
def trained(market: Path) -> Path:
    """MLflow's store, with the first bundle trained into its models/ folder."""
    store = market / "store"
    _train(market, store, store / "models")
    return store


def _bundle_folder(trained: Path) -> Path:
    (folder,) = (trained / "models").iterdir()
    return folder


def test_train_writes_the_bundle_into_a_folder_named_for_its_version(trained: Path):
    folder = _bundle_folder(trained)

    bundle = ModelBundle.load(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))

    assert folder.name == bundle.version == manifest["version"]
    assert manifest["fitted_on"]["labels_known_on_day"] == 14


def test_train_records_the_run_and_registers_the_bundle_in_mlflow(trained: Path):
    bundle = ModelBundle.load(_bundle_folder(trained))
    mlflow.set_tracking_uri(f"sqlite:///{trained / 'mlflow.db'}")
    client = MlflowClient()

    version = client.get_model_version(MODEL_NAME, "1")
    run = client.get_run(version.run_id)

    assert version.tags["bundle_version"] == bundle.version
    assert run.data.params["seed"] == "42"
    assert run.data.params["splits.train"] == "[0, 8]"
    assert run.data.metrics["threshold_high"] == pytest.approx(bundle.thresholds.high)
    assert run.data.metrics["stacker_weight_supervised_log_odds"] == pytest.approx(
        bundle.stacker.weights["supervised_log_odds"]
    )
    artifacts = {a.path for a in client.list_artifacts(run.info.run_id, "bundle")}
    assert artifacts == {"bundle/bundle.pkl", "bundle/manifest.json"}
    assert Path(version.source.removeprefix("file://")).is_relative_to(trained / "mlruns")


def test_each_training_run_registers_a_new_version(market: Path, trained: Path):
    _train(market, trained, market / "retrained")

    mlflow.set_tracking_uri(f"sqlite:///{trained / 'mlflow.db'}")
    versions = MlflowClient().search_model_versions(f"name='{MODEL_NAME}'")
    assert sorted(int(v.version) for v in versions) == [1, 2]


@pytest.fixture(scope="module")
def stacked(market: Path) -> Path:
    out = market / "stacked"
    assert main(["evaluate", *_options(market), "--scorer", "stacked", "--out", str(out)]) == 0
    return out


def test_the_stacked_risk_score_is_judged_like_any_other(stacked: Path):
    metrics = json.loads((stacked / "metrics.json").read_text(encoding="utf-8"))
    scores = pd.read_parquet(stacked / "scores.parquet")

    assert metrics["scorer"] == "stacked"
    assert len(scores) == metrics["transactions"]
    assert scores["risk_score"].between(0, 100).all()
    # High is set to the Alert budget (20 a day) on days 11-14, like every scorer's.
    assert 10 <= metrics["at_high"]["per_day"] <= 40
    assert scores.loc[scores["critical"], "risk_band"].eq("High").all()


def test_the_stacked_scores_name_the_bundle_txrisk_train_gives(stacked: Path, trained: Path):
    scores = pd.read_parquet(stacked / "scores.parquet")

    assert set(scores["model_version"]) == {_bundle_folder(trained).name}


def test_each_component_is_kept_beside_the_stacked_risk_score(stacked: Path):
    scores = pd.read_parquet(stacked / "scores.parquet")

    for component in ("rules_score", "anomaly_score", "supervised_probability"):
        assert scores[component].between(0, 1).all()
    # Below the Critical floor, the Risk score is the stacker's probability x 100.
    plain = ~scores["critical"]
    assert scores.loc[plain, "risk_score"].to_numpy() == pytest.approx(
        scores.loc[plain, "fraud_probability"].to_numpy() * 100
    )


def test_the_stacked_probability_gets_a_reliability_check(stacked: Path):
    metrics = json.loads((stacked / "metrics.json").read_text(encoding="utf-8"))

    # Fitted on Fraud labels, it is the chance of fraud that gets discovered, like the
    # supervised probability; there is no raw probability to compare it with.
    for against in ("eventually_discovered", "ground_truth"):
        assert set(metrics["reliability"][against]) == {"calibrated"}
