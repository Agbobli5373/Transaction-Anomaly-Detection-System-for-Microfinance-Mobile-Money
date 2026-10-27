"""Risk band thresholds set from the Alert budget: an operational limit, not a round number."""

from dataclasses import dataclass

import numpy as np
import pandas as pd

# Medium covers this many times the Alert budget beyond High.
MEDIUM_MULTIPLE = 3


@dataclass(frozen=True)
class BandThresholds:
    medium: float
    high: float


def _score_at_rank(descending: np.ndarray, rank: int) -> float:
    return float(descending[min(rank, len(descending)) - 1])


def budget_thresholds(
    scores: pd.Series, *, days: int, budget_per_day: int, already_high: int = 0
) -> BandThresholds:
    """Thresholds that, over these `days` of scores, give the Alert budget's worth of High.

    High starts at the score of the budget's last Alert; Medium covers the next
    `MEDIUM_MULTIPLE` times as many. `already_high` Alerts are High regardless of score (Critical
    rule hits) and are not among `scores`, so they use up budget first. Scores need no labels,
    so any recent days will do.
    """
    descending = np.sort(scores.to_numpy(dtype=np.float64))[::-1]
    alerts = max(budget_per_day * days - already_high, 1)
    return BandThresholds(
        medium=_score_at_rank(descending, alerts * (1 + MEDIUM_MULTIPLE)),
        high=_score_at_rank(descending, alerts),
    )


def band(scores: pd.Series, thresholds: BandThresholds) -> pd.Series:
    """The Risk band of each score: High from the high threshold, Medium from the medium one."""
    bands = np.where(
        scores >= thresholds.high,
        "High",
        np.where(scores >= thresholds.medium, "Medium", "Low"),
    )
    return pd.Series(bands, index=scores.index, dtype="string")
