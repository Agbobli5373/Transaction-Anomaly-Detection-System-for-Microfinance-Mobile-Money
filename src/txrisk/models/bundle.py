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
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from txrisk.evaluation import EvaluationConfig, Split, split_of, training_labels
from txrisk.evaluation.thresholds import BandThresholds, budget_thresholds
from txrisk.models.anomaly import AnomalyModel
from txrisk.models.card import build_card, write_card
from txrisk.models.inputs import INPUT_COLUMNS, model_inputs
from txrisk.models.stacked import Stacker, stack_components
from txrisk.models.supervised import SupervisedModel, train_supervised
from txrisk.monitoring.psi import Baseline, fit_baseline
from txrisk.scoring.config import RulesConfig
from txrisk.scoring.service import RISK_SCORE_DECIMALS
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
    # What monitoring compares live inputs and Risk scores with: their distribution on rows the
    # bundle was built from, never later ones. None for a bundle trained before baselines (an old
    # pickle has no such attribute, and reads as this default): monitoring reports 'no baseline'.
    baseline: Baseline | None = None
    # The model card (see `txrisk.models.card`), written when the bundle is trained. None for a
    # bundle trained before cards existed, which reads as this default.
    card: dict[str, Any] | None = None

    def components(self, inputs: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
        return model_components(self.anomaly, self.supervised, inputs, rules)

    def contributions(self, inputs: pd.DataFrame) -> pd.DataFrame:
        """How much each input pushed each row's Risk score, in the stacker's log-odds.

        Each model's contributions are weighted as the stacker weighs that model. Together they
        add up to the models' part of the Risk score's log-odds, less a constant; the rules' part
        is explained by the Rule hits themselves.
        """
        weights = self.stacker.weights
        supervised = self.supervised.contributions(inputs)[self.input_columns]
        anomaly = self.anomaly.contributions(inputs)[self.input_columns]
        return weights["supervised_log_odds"] * supervised + weights["anomaly"] * anomaly

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
            "baseline": None if self.baseline is None else self.baseline.to_json(),
        }
        (folder / _MANIFEST_FILE).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        write_card(self.card or build_card(self), folder)

    @classmethod
    def load(cls, folder: Path) -> "ModelBundle":
        """A bundle saved by `save`. Only load bundles this system wrote: they are pickles."""
        bundle = pickle.loads((folder / _MODEL_FILE).read_bytes())
        if not isinstance(bundle, cls):
            raise TypeError(f"{folder} does not hold a model bundle")
        return bundle


def bundle_version(folder: Path) -> str:
    """The version of the bundle saved in `folder`: from its manifest, which is cheap to read,
    else by loading it."""
    try:
        version = json.loads((folder / _MANIFEST_FILE).read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return ModelBundle.load(folder).version
    return str(version)


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
    rule_scores: pd.DataFrame | None = None,
    baseline_bins: int = 10,
    wallets: pd.DataFrame | None = None,
) -> ModelBundle:
    """Train every part of a bundle. `rule_scores` is `rule_scores_table` of these Transactions
    if already computed; without it it is computed here, a Python loop over every row. `wallets`
    adds the KYC tier and Wallet age band to the model card's segments."""
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
    rules_scores = (
        rule_scores_table(transactions, feature_rows, rules) if rule_scores is None else rule_scores
    )

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
    bundle = ModelBundle(
        version="",
        anomaly=anomaly,
        supervised=supervised,
        stacker=stacker,
        thresholds=thresholds,
        input_columns=list(INPUT_COLUMNS),
        fitted_on=fitted_on,
        metrics=metrics,
    )
    # The baseline monitoring measures drift against: the inputs and Risk scores of the threshold
    # days, the latest days before the Champion goes live. Nothing from the evaluation days, whose
    # drift it exists to detect, and the Risk scores are the bundle's own (Critical floor included).
    # Risk scores are stored to RISK_SCORE_DECIMALS, which is all monitoring can see of a live
    # one, so the baseline is binned on them as stored.
    recent_risk = bundle.risk_scores(recent, rules_scores)
    baseline = fit_baseline(
        recent[INPUT_COLUMNS],
        recent_risk.round(RISK_SCORE_DECIMALS),
        bins=baseline_bins,
        alerted=(recent_risk >= thresholds.high).to_numpy(),
    )
    final = replace(
        bundle,
        baseline=baseline,
        version=_version(anomaly, supervised, stacker, thresholds, fitted_on, baseline),
    )
    card = build_card(
        final,
        data={
            "transactions_by_split": {
                name.value: int((split == name).fillna(False).sum()) for name in Split
            },
            "fraud_labels_known": int((discoveries["discovered_at"] < cutoff).sum()),
            "label_maturity_days": config.label_maturity_days,
            "alert_budget_per_day": config.alert_budget_per_day,
        },
        metrics=_segment_metrics(
            final, transactions, wallets, labelled, labels, rules_scores, config=config
        ),
    )
    return replace(final, card=card)


def _segment_metrics(
    bundle: ModelBundle,
    transactions: pd.DataFrame,
    wallets: pd.DataFrame | None,
    labelled: pd.DataFrame,
    labels: pd.Series,
    rules: pd.DataFrame,
    *,
    config: EvaluationConfig,
) -> dict[str, Any] | None:
    """The bundle's precision, recall and false positive rate at High on the calibration days,
    against the Fraud labels known when it goes live, overall and by segment for the model card;
    None if no calibration Transaction has a label."""
    from txrisk.evaluation.metrics import at_threshold, ranking
    from txrisk.evaluation.report import _segments, _wallet_age_band

    if labelled.empty:
        return None
    frame = (
        transactions.set_index("transaction_id")
        .loc[labelled.index, ["occurred_at", "subject_wallet_id", "region", "channel"]]
        .assign(fraud=labels.loc[labelled.index].astype(bool))
    )
    segments = ["region", "channel"]
    if wallets is not None:
        frame = frame.join(
            wallets.set_index("wallet_id")[["kyc_tier", "registered_at"]], on="subject_wallet_id"
        )
        age = (frame["occurred_at"] - frame["registered_at"]).dt.total_seconds() / 86_400
        frame["wallet_age_band"] = _wallet_age_band(age, config.wallet_age_band_edges)
        segments = ["region", "kyc_tier", "channel", "wallet_age_band"]
    scores = bundle.risk_scores(labelled, rules)
    high = scores >= bundle.thresholds.high
    overall = at_threshold(frame["fraud"], high)
    rank = ranking(frame["fraud"], scores)
    return {
        "evaluated_on": (
            "The calibration days, against the Fraud labels known when this version goes live, "
            "at its High threshold. The stacker was fitted on these days, so they flatter it."
        ),
        "overall": {
            "transactions": len(frame),
            "fraud": int(frame["fraud"].sum()),
            **asdict(overall),
            "pr_auc": rank.pr_auc,
            "roc_auc": rank.roc_auc,
        },
        "by_segment": _segments(frame, high, tuple(segments)),
    }
