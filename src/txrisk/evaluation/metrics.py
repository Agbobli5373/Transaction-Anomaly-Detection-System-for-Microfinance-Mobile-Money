"""The measures a fraud operation judges a scorer by."""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score


@dataclass(frozen=True)
class ThresholdMetrics:
    flagged: int
    precision: float | None  # None when nothing is flagged
    recall: float | None  # None when there is no fraud to find
    false_positive_rate: float | None  # None when there is nothing legitimate


@dataclass(frozen=True)
class RankingMetrics:
    roc_auc: float | None  # None unless both fraud and legitimate are present
    pr_auc: float | None


@dataclass(frozen=True)
class BudgetMetrics:
    alerts: int
    precision: float | None


@dataclass(frozen=True)
class ReliabilityBin:
    lower: float
    upper: float
    transactions: int
    mean_probability: float
    fraud_rate: float


@dataclass(frozen=True)
class Reliability:
    brier_score: float
    expected_calibration_error: float  # the bins' gaps, weighted by how many Transactions each has
    bins: list[ReliabilityBin]  # empty bins are left out


def _share(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def at_threshold(fraud: pd.Series, flagged: pd.Series) -> ThresholdMetrics:
    """Precision, recall and false positive rate of the Transactions flagged."""
    fraud, flagged = fraud.astype(bool), flagged.astype(bool)
    caught = int((fraud & flagged).sum())
    false_alarms = int((~fraud & flagged).sum())
    return ThresholdMetrics(
        flagged=int(flagged.sum()),
        precision=_share(caught, caught + false_alarms),
        recall=_share(caught, int(fraud.sum())),
        false_positive_rate=_share(false_alarms, int((~fraud).sum())),
    )


def ranking(fraud: pd.Series, score: pd.Series) -> RankingMetrics:
    """How well the score ranks fraud above legitimate Transactions, at every threshold."""
    if fraud.nunique() < 2:
        return RankingMetrics(roc_auc=None, pr_auc=None)
    return RankingMetrics(
        roc_auc=float(roc_auc_score(fraud, score)),
        pr_auc=float(average_precision_score(fraud, score)),
    )


def precision_at_budget(
    fraud: pd.Series, score: pd.Series, day: pd.Series, *, budget_per_day: int
) -> BudgetMetrics:
    """The share of fraud among the highest-scored `budget_per_day` Transactions of each day.

    This is what reviewers would find if they could look at exactly that many Alerts a day. Ties
    are broken by Transaction order, so the result is deterministic.
    """
    frame = pd.DataFrame({"fraud": fraud.astype(bool), "score": score, "day": day})
    ranked = frame.sort_values("score", ascending=False, kind="stable")
    alerts = ranked.groupby("day", sort=False).head(budget_per_day)
    return BudgetMetrics(
        alerts=len(alerts), precision=_share(int(alerts["fraud"].sum()), len(alerts))
    )


def reliability(fraud: pd.Series, probability: pd.Series, *, bins: int = 10) -> Reliability:
    """How closely a fraud probability matches how often fraud actually happens.

    Probabilities are grouped into `bins` equal-width bins over [0, 1]; a well-calibrated
    probability has each bin's fraud rate close to its mean probability.
    """
    actual = fraud.astype(float).to_numpy()
    predicted = probability.astype(float).to_numpy()
    edges = np.linspace(0, 1, bins + 1)
    which = np.clip(np.digitize(predicted, edges[1:-1]), 0, bins - 1)
    found: list[ReliabilityBin] = []
    gap = 0.0
    for b in range(bins):
        inside = which == b
        if not inside.any():
            continue
        mean_probability = float(predicted[inside].mean())
        fraud_rate = float(actual[inside].mean())
        found.append(
            ReliabilityBin(
                lower=float(edges[b]),
                upper=float(edges[b + 1]),
                transactions=int(inside.sum()),
                mean_probability=mean_probability,
                fraud_rate=fraud_rate,
            )
        )
        gap += inside.sum() * abs(mean_probability - fraud_rate)
    return Reliability(
        brier_score=float(np.mean((predicted - actual) ** 2)),
        expected_calibration_error=gap / len(predicted),
        bins=found,
    )
