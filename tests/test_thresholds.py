"""Risk band thresholds set from the Alert budget, on scores alone."""

import numpy as np
import pandas as pd
import pytest

from txrisk.evaluation.thresholds import BandThresholds, band, budget_thresholds


def test_high_starts_where_the_budget_runs_out_and_medium_covers_three_times_more():
    # Two days, an Alert budget of 2 a day: the 4 highest scores are High, and Medium covers
    # three times that many more, the next 12.
    scores = pd.Series(range(100, 0, -5))  # 100, 95, ..., 5

    thresholds = budget_thresholds(scores, days=2, budget_per_day=2)

    assert thresholds == BandThresholds(medium=25.0, high=85.0)


def test_bands_follow_the_thresholds():
    thresholds = BandThresholds(medium=45.0, high=85.0)

    assert band(pd.Series([90.0, 85.0, 84.9, 45.0, 44.9]), thresholds).tolist() == [
        "High",
        "High",
        "Medium",
        "Medium",
        "Low",
    ]


def test_when_critical_rule_hits_use_up_the_budget_no_scored_transaction_is_high():
    scores = pd.Series(range(100, 0, -5))

    # 4 Alerts of budget, already taken by 5 Critical rule hits.
    thresholds = budget_thresholds(scores, days=2, budget_per_day=2, already_high=5)

    assert (band(scores, thresholds) != "High").all()
    # Medium still covers three times the budget.
    assert (band(scores, thresholds) == "Medium").sum() == 12


def test_thresholds_need_scores_to_set_them_from():
    with pytest.raises(ValueError, match="no scores"):
        budget_thresholds(pd.Series(dtype=float), days=2, budget_per_day=2)


def _busy_day_thresholds() -> BandThresholds:
    # Two days, an Alert budget of 2 a day: 4 Alerts at High, and up to twice as few on a busy day.
    scores = pd.Series(range(100, 0, -5))
    return budget_thresholds(
        scores, days=2, budget_per_day=2, surge_elasticity=1.0, surge_max_divisor=2.0
    )


def test_high_starts_higher_when_the_market_is_busier():
    thresholds = _busy_day_thresholds()

    on_the_day = thresholds.high_at(np.array([1.0, 1.5, 2.0]))

    # A normal day keeps the budget's 4 Alerts; 1.5 times the spending leaves room for 3 (the
    # budget divided by 1.5, rounded), and twice the spending for 2.
    assert on_the_day.tolist() == [85.0, 90.0, 95.0]
    assert (thresholds.medium, thresholds.high) == (25.0, 85.0)


def test_a_quiet_day_or_an_unknown_market_keeps_the_plain_high_threshold():
    thresholds = _busy_day_thresholds()

    assert thresholds.high_at(np.array([0.4, np.nan])).tolist() == [85.0, 85.0]


def test_high_never_rises_past_the_largest_configured_budget_reduction():
    thresholds = _busy_day_thresholds()

    assert thresholds.high_at(np.array([2.0, 9.0])).tolist() == [95.0, 95.0]


def test_the_elasticity_sets_how_fast_high_rises_with_the_market():
    scores = pd.Series(range(100, 0, -5))
    steeper = budget_thresholds(
        scores, days=2, budget_per_day=2, surge_elasticity=2.0, surge_max_divisor=2.0
    )

    # 1.5 ** 2 = 2.25 is past the limit of 2: the budget is cut as far as it may go.
    assert steeper.high_at(np.array([1.5])).tolist() == [95.0]


def test_without_an_elasticity_high_never_moves():
    thresholds = budget_thresholds(pd.Series(range(100, 0, -5)), days=2, budget_per_day=2)

    assert thresholds.surge is None
    assert thresholds.high_at(np.array([3.0])).tolist() == [85.0]


def test_a_busy_market_moves_the_high_band_but_not_medium_or_critical_rule_hits():
    thresholds = _busy_day_thresholds()
    # The last is a Critical rule hit, whose score is floored at the plain High threshold.
    scores = pd.Series([97.0, 90.0, 87.0, 60.0, 85.0])
    critical = pd.Series([False, False, False, False, True])

    normal = band(scores, thresholds, market_index=pd.Series([1.0] * 5), critical=critical)
    busy = band(scores, thresholds, market_index=pd.Series([2.0] * 5), critical=critical)

    assert normal.tolist() == ["High", "High", "High", "Medium", "High"]
    # High now needs 95: 90 and 87 drop to Medium, which keeps them on the watch list.
    assert busy.tolist() == ["High", "Medium", "Medium", "Medium", "High"]
