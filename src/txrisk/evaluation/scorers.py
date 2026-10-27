"""Scorers the evaluation can judge, each giving Risk scores for the evaluation split."""

from datetime import date

import pandas as pd

from txrisk.evaluation.config import EvaluationConfig
from txrisk.evaluation.labels import Split, split_of
from txrisk.evaluation.thresholds import band, budget_thresholds
from txrisk.features import FeatureConfig, feature_table
from txrisk.models import model_inputs
from txrisk.models.anomaly import AnomalyModel
from txrisk.scoring import RulesConfig, score_table

ANOMALY_SEED = 42


def rules_only_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    features: FeatureConfig,
    rules: RulesConfig,
    evaluation: EvaluationConfig,
    period_start: date,
) -> pd.DataFrame:
    """The rules alone, with their own fixed Risk bands."""
    start = pd.Timestamp(period_start, tz="UTC")
    return score_table(
        transactions,
        wallets,
        sim_swaps,
        features=features,
        rules=rules,
        score_from=start + pd.Timedelta(days=evaluation.splits.evaluation.start),
    )


def anomaly_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    features: FeatureConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    method: str = "isolation_forest",
) -> pd.DataFrame:
    """The Anomaly score alone: fitted on the training days with no labels, banded at the Alert
    budget on the threshold days, and scored on the evaluation days. The Risk score is the
    Anomaly score x 100."""
    inputs = model_inputs(
        transactions, feature_table(transactions, wallets, sim_swaps, config=features)
    )
    split = split_of(transactions["occurred_at"], period_start, evaluation).to_numpy()
    training = inputs[split == Split.TRAIN]
    if method == "isolation_forest":
        model = AnomalyModel.fit(training, seed=ANOMALY_SEED)
    else:
        model = AnomalyModel.fit_lof(training, seed=ANOMALY_SEED)

    window = evaluation.splits.thresholds
    thresholds = budget_thresholds(
        model.score(inputs[split == Split.THRESHOLDS]) * 100,
        days=window.end - window.start,
        budget_per_day=evaluation.alert_budget_per_day,
    )
    anomaly = model.score(inputs[split == Split.EVALUATION])
    risk_score = anomaly * 100
    return pd.DataFrame(
        {
            "transaction_id": anomaly.index.to_numpy(),
            "risk_score": risk_score.to_numpy(),
            "risk_band": band(risk_score, thresholds).to_numpy(),
            "anomaly_score": anomaly.to_numpy(),
        }
    ).astype({"transaction_id": "string", "risk_band": "string"})
