"""The model bundle: everything one model version needs to turn inputs into a Risk score.

Both models, the stacker and the Risk band thresholds are trained together and shipped together,
so a Risk score can always be traced to one version. Training follows ADR 0007 and ADR 0008: the
models learn from the train days and the stacker from the calibration days, both with only the
labels known when the evaluation days start; the thresholds come from the Alert budget on the
threshold days, which needs no labels.
"""

import hashlib
import json
import pickle
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from txrisk.evaluation import EvaluationConfig, Split, split_of, training_labels
from txrisk.evaluation.thresholds import BandThresholds, budget_thresholds
from txrisk.models.anomaly import AnomalyModel
from txrisk.models.inputs import INPUT_COLUMNS, model_inputs
from txrisk.models.stacked import Stacker, stack_components
from txrisk.models.supervised import SupervisedModel, train_supervised
from txrisk.scoring.config import RulesConfig
from txrisk.scoring.table import rule_scores_table

_MODEL_FILE = "bundle.pkl"
_MANIFEST_FILE = "manifest.json"


def model_components(
    anomaly: AnomalyModel, supervised: SupervisedModel, inputs: pd.DataFrame, rules: pd.DataFrame
) -> pd.DataFrame:
    """Each component score of each row of inputs: the stacker's three inputs, plus the
    supervised probability raw and calibrated."""
    raw = supervised.raw_probability(inputs)
    stacked = stack_components(
        rules_score=rules["rules_score"].loc[inputs.index],
        anomaly_score=anomaly.score(inputs),
        raw_probability=raw,
    )
    return stacked.assign(fraud_probability=supervised.score(inputs), raw_probability=raw)


@dataclass(frozen=True)
class ModelBundle:
    version: str
    anomaly: AnomalyModel
    supervised: SupervisedModel
    stacker: Stacker
    thresholds: BandThresholds
    input_columns: list[str]
    # The days each part was fitted on, and the day whose labels training used.
    fitted_on: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)

    def components(self, inputs: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
        return model_components(self.anomaly, self.supervised, inputs, rules)

    def risk_scores(
        self, inputs: pd.DataFrame, rules: pd.DataFrame, components: pd.DataFrame | None = None
    ) -> pd.Series:
        """The Risk score of each row, 0-100. A Critical rule hit puts it at least at High."""
        if components is None:
            components = self.components(inputs, rules)
        scores = self.stacker.score(components)
        critical = rules["critical"].loc[inputs.index].to_numpy(dtype=bool)
        return scores.where(~critical, np.maximum(scores, self.thresholds.high))

    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _MODEL_FILE).write_bytes(pickle.dumps(self))
        manifest = {
            "version": self.version,
            "fitted_on": self.fitted_on,
            "thresholds": {"medium": self.thresholds.medium, "high": self.thresholds.high},
            "stacker": {"weights": self.stacker.weights, "intercept": self.stacker.intercept},
            "input_columns": self.input_columns,
            "metrics": self.metrics,
        }
        (folder / _MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, folder: Path) -> "ModelBundle":
        """A bundle saved by `save`. Only load bundles this system wrote: they are pickles."""
        bundle = pickle.loads((folder / _MODEL_FILE).read_bytes())  # noqa: S301
        if not isinstance(bundle, cls):
            raise TypeError(f"{folder} does not hold a model bundle")
        return bundle


def _version(*parts: object) -> str:
    """A version that changes exactly when anything trained changes."""
    return "risk-" + hashlib.sha256(pickle.dumps(parts)).hexdigest()[:12]


def train_bundle(
    transactions: pd.DataFrame,
    feature_rows: pd.DataFrame,
    discoveries: pd.DataFrame,
    *,
    rules: RulesConfig,
    period_start: date,
    config: EvaluationConfig,
    seed: int,
) -> ModelBundle:
    splits = config.splits
    split = split_of(transactions["occurred_at"], period_start, config)
    inputs = model_inputs(transactions, feature_rows)

    def rows_in(name: Split) -> pd.DataFrame:
        # Transactions outside every split have no split and are never in one.
        in_split: pd.DataFrame = inputs[(split == name).fillna(False).to_numpy(dtype=bool)]
        return in_split

    anomaly = AnomalyModel.fit(rows_in(Split.TRAIN), seed=seed)
    supervised = train_supervised(
        transactions, feature_rows, discoveries, period_start=period_start, config=config, seed=seed
    )
    rules_scores = rule_scores_table(transactions, feature_rows, rules)

    # The stacker learns from the calibration days, with the labels known on the day it goes live.
    cutoff = pd.Timestamp(period_start, tz="UTC") + pd.Timedelta(days=splits.evaluation.start)
    labels = training_labels(transactions, discoveries, cutoff=cutoff, config=config)
    calibration = rows_in(Split.CALIBRATION)
    labelled = calibration.loc[calibration.index.isin(labels.index)]
    stacker = Stacker.fit(
        model_components(anomaly, supervised, labelled, rules_scores),
        labels.loc[labelled.index].astype(bool),
    )

    # Thresholds from the Alert budget on the threshold days. Critical rule hits are High
    # regardless of score, so they take their share of the budget first.
    recent = rows_in(Split.THRESHOLDS)
    recent_scores = stacker.score(model_components(anomaly, supervised, recent, rules_scores))
    critical = rules_scores["critical"].loc[recent.index].to_numpy(dtype=bool)
    thresholds = budget_thresholds(
        recent_scores[~critical],
        days=splits.thresholds.end - splits.thresholds.start,
        budget_per_day=config.alert_budget_per_day,
        already_high=int(critical.sum()),
    )

    fitted_on = {
        "models": [splits.train.start, splits.train.end],
        "stacker": [splits.calibration.start, splits.calibration.end],
        "thresholds": [splits.thresholds.start, splits.thresholds.end],
        "labels_known_on_day": splits.evaluation.start,
    }
    metrics = {
        "stacker_transactions": len(labelled),
        "stacker_fraud": int(labels.loc[labelled.index].sum()),
        "threshold_transactions": len(recent),
        "critical_on_threshold_days": int(critical.sum()),
    }
    return ModelBundle(
        version=_version(anomaly, supervised, stacker, thresholds, fitted_on),
        anomaly=anomaly,
        supervised=supervised,
        stacker=stacker,
        thresholds=thresholds,
        input_columns=list(INPUT_COLUMNS),
        fitted_on=fitted_on,
        metrics=metrics,
    )
