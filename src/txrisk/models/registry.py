"""Tracking training runs in MLflow: what each run was given, what it produced, and the bundle,
registered as a new version of one model.

The bundle folder stays the thing that is served; MLflow is the record of how each version was
made and the list of versions to choose from.
"""

from pathlib import Path
from typing import Any

import mlflow
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from txrisk.models.bundle import ModelBundle

EXPERIMENT = "txrisk"
MODEL_NAME = "txrisk-risk-score"
_ARTIFACT_PATH = "bundle"


def _experiment_id(client: MlflowClient, artifacts: Path) -> str:
    experiment = client.get_experiment_by_name(EXPERIMENT)
    if experiment is not None:
        return str(experiment.experiment_id)
    return str(client.create_experiment(EXPERIMENT, artifact_location=artifacts.resolve().as_uri()))


def _metrics(bundle: ModelBundle) -> dict[str, float]:
    metrics = {
        "threshold_medium": bundle.thresholds.medium,
        "threshold_high": bundle.thresholds.high,
        "stacker_intercept": bundle.stacker.intercept,
        **{f"stacker_weight_{name}": w for name, w in bundle.stacker.weights.items()},
        **bundle.metrics,
    }
    return {name: float(value) for name, value in metrics.items()}


def log_bundle(
    bundle: ModelBundle,
    folder: Path,
    *,
    tracking_uri: str,
    artifacts: Path,
    params: dict[str, Any],
) -> str:
    """Record one training run of `bundle`, saved in `folder`, and register it as a new version
    of the model. Returns that version's number.

    `artifacts` is where a new experiment stores run artifacts; an existing one keeps its own.
    """
    mlflow.set_tracking_uri(tracking_uri)
    client = MlflowClient()
    with mlflow.start_run(experiment_id=_experiment_id(client, artifacts), run_name=bundle.version):
        run = mlflow.active_run()
        assert run is not None
        mlflow.log_params({**params, "bundle_version": bundle.version})
        mlflow.log_metrics(_metrics(bundle))
        mlflow.log_artifacts(str(folder), artifact_path=_ARTIFACT_PATH)
        try:
            client.create_registered_model(MODEL_NAME)
        except MlflowException as error:
            if error.error_code != "RESOURCE_ALREADY_EXISTS":
                raise
        version = client.create_model_version(
            MODEL_NAME,
            source=f"{run.info.artifact_uri}/{_ARTIFACT_PATH}",
            run_id=run.info.run_id,
            tags={"bundle_version": bundle.version},
        )
    return str(version.version)
