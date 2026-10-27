"""Risk band thresholds set from the Alert budget, on scores alone."""

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
