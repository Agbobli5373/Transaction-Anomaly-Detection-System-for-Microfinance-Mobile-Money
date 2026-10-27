"""The measures a fraud operation judges a scorer by."""

from dataclasses import dataclass

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
