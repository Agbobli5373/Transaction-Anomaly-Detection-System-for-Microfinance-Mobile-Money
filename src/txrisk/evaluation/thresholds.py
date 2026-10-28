"""Risk band thresholds set from the Alert budget: an operational limit, not a round number."""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Medium covers this many times the Alert budget beyond High.
MEDIUM_MULTIPLE = 3
# The surge divisors at which the High threshold is kept, from 1 up to the configured maximum.
SURGE_STEP = 0.05


@dataclass(frozen=True)
class SurgeThresholds:
    """How the High threshold rises when the whole market is busier and spending more (ADR 0010).

    On a day when the market spend index is `index` (1 is a normal day) the Alert budget is shared
    among that many times more spending, so High is set as if the budget were `index ** elasticity`
    times smaller: the score of the budget's last Alert at each such divisor, from the threshold
    days. Between kept divisors the threshold is interpolated.
    """

    elasticity: float
    divisors: tuple[float, ...]
    highs: tuple[float, ...]

    def high(self, market_index: np.ndarray) -> np.ndarray:
        """The High threshold at each market spend index (all above 1)."""
        divisor = np.minimum(market_index**self.elasticity, self.divisors[-1])
        return np.asarray(np.interp(divisor, self.divisors, self.highs))


@dataclass(frozen=True)
class BandThresholds:
    medium: float
    high: float
    # None: High never moves (a bundle trained before #47, or with no elasticity configured).
    surge: SurgeThresholds | None = None

    def high_at(self, market_index: np.ndarray) -> np.ndarray:
        """The High threshold at each market spend index; the plain `high` where it is unknown or
        the market is no busier than usual."""
        high = np.full(len(market_index), self.high)
        if self.surge is not None:
            busier = market_index > 1.0  # false for an unknown (NaN) index
            high[busier] = self.surge.high(market_index[busier])
        return high


def _score_at_rank(descending: np.ndarray, rank: int) -> float:
    return float(descending[min(rank, len(descending)) - 1])


def budget_thresholds(
    scores: pd.Series,
    *,
    days: int,
    budget_per_day: int,
    already_high: int = 0,
    surge_elasticity: float = 0.0,
    surge_max_divisor: float = 1.0,
) -> BandThresholds:
    """Thresholds that, over these `days` of scores, give the Alert budget's worth of High.

    High starts at the score of the budget's last Alert; Medium covers the next
    `MEDIUM_MULTIPLE` times as many. `already_high` Alerts are High regardless of score (Critical
    rule hits) and are not among `scores`, so they use up budget first. Scores need no labels,
    so any recent days will do.

    With a `surge_elasticity` above 0 the thresholds also say how High rises on a busy day, up to
    a budget `surge_max_divisor` times smaller (see `SurgeThresholds`). The Critical rule hits
    stay High whatever the day, so only the budget left after them is divided.
    """
    if scores.empty:
        raise ValueError("no scores to set Risk band thresholds from")
    descending = np.sort(scores.to_numpy(dtype=np.float64))[::-1]
    budget = budget_per_day * days
    alerts = budget - already_high
    if alerts <= 0:
        # Critical rule hits alone fill the budget: no scored Transaction is High.
        return BandThresholds(
            medium=_score_at_rank(descending, budget * MEDIUM_MULTIPLE), high=math.inf
        )
    surge = None
    if surge_elasticity > 0 and surge_max_divisor > 1:
        steps = round((surge_max_divisor - 1) / SURGE_STEP)
        divisors = tuple(round(1 + SURGE_STEP * i, 10) for i in range(steps + 1))
        surge = SurgeThresholds(
            elasticity=surge_elasticity,
            divisors=divisors,
            highs=tuple(_score_at_rank(descending, max(1, round(alerts / d))) for d in divisors),
        )
    return BandThresholds(
        medium=_score_at_rank(descending, alerts * (1 + MEDIUM_MULTIPLE)),
        high=_score_at_rank(descending, alerts),
        surge=surge,
    )


def band(
    scores: pd.Series,
    thresholds: BandThresholds,
    *,
    market_index: pd.Series | None = None,
    critical: pd.Series | None = None,
) -> pd.Series:
    """The Risk band of each score: High from the high threshold, Medium from the medium one.

    With a `market_index` per score, High starts at the threshold for that row's market spend
    index (empty counts as a normal day). A `critical` row is judged by the plain threshold: its
    score is floored there, so it is High whatever the market.
    """
    if market_index is None:
        high = np.full(len(scores), thresholds.high)
    else:
        high = thresholds.high_at(market_index.to_numpy(dtype=np.float64))
    if critical is not None:
        high = np.where(critical.to_numpy(dtype=bool), thresholds.high, high)
    is_high = scores.to_numpy() >= high
    bands = np.where(
        is_high,
        "High",
        np.where(scores >= thresholds.medium, "Medium", "Low"),
    )
    return pd.Series(bands, index=scores.index, dtype="string")
