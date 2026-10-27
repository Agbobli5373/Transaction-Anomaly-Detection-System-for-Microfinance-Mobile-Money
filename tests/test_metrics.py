"""Metrics, checked against values worked out by hand for eight Transactions over two days."""

import pandas as pd
import pytest

from txrisk.evaluation.metrics import at_threshold, precision_at_budget, ranking, reliability

#           0     1     2     3     4     5     6     7
FRAUD = pd.Series([1, 1, 0, 0, 0, 0, 1, 0])
SCORE = pd.Series([90, 60, 55, 10, 5, 0, 20, 80])
DAY = pd.Series(["d1"] * 4 + ["d2"] * 4)


def test_metrics_at_a_threshold():
    flagged = SCORE >= 50  # 0, 1, 2 and 7: two fraud, two not; fraud 6 is missed

    metrics = at_threshold(FRAUD, flagged)

    assert metrics.flagged == 4
    assert metrics.precision == 0.5
    assert metrics.recall == pytest.approx(2 / 3)
    assert metrics.false_positive_rate == pytest.approx(2 / 5)


def test_ranking_metrics():
    metrics = ranking(FRAUD, SCORE)

    # Of the 3 x 5 fraud/legitimate pairs, the fraud scores higher in 5 + 4 + 3.
    assert metrics.roc_auc == pytest.approx(12 / 15)
    # Ranked by score, fraud sits at ranks 1, 3 and 5: precision 1, 2/3 and 3/5 there.
    assert metrics.pr_auc == pytest.approx((1 + 2 / 3 + 3 / 5) / 3)


def test_precision_when_reviewers_take_the_top_alerts_each_day():
    # One Alert a day: Transaction 0 on day 1 (fraud) and Transaction 7 on day 2 (not).
    metrics = precision_at_budget(FRAUD, SCORE, DAY, budget_per_day=1)

    assert metrics.alerts == 2
    assert metrics.precision == 0.5


def test_how_well_a_probability_matches_how_often_fraud_happens():
    probability = SCORE / 100

    metrics = reliability(FRAUD, probability, bins=2)

    # Squared misses: 0.1, 0.4, 0.55, 0.1, 0.05, 0, 0.8 and 0.8.
    assert metrics.brier_score == pytest.approx(1.765 / 8)
    # Below 0.5: 0.1, 0.05, 0 and 0.2 average 0.0875, and one in four is fraud. From 0.5: 0.9,
    # 0.6, 0.55 and 0.8 average 0.7125, and two in four are fraud.
    assert [(b.lower, b.upper, b.transactions) for b in metrics.bins] == [(0, 0.5, 4), (0.5, 1, 4)]
    assert [b.mean_probability for b in metrics.bins] == pytest.approx([0.0875, 0.7125])
    assert [b.fraud_rate for b in metrics.bins] == pytest.approx([0.25, 0.5])
    assert metrics.expected_calibration_error == pytest.approx(0.5 * 0.1625 + 0.5 * 0.2125)


def test_metrics_are_empty_rather_than_misleading_when_undefined():
    nothing_flagged = at_threshold(FRAUD, SCORE > 100)
    no_fraud = ranking(pd.Series([0, 0, 0]), pd.Series([1, 2, 3]))

    assert nothing_flagged.precision is None
    assert nothing_flagged.recall == 0.0
    assert no_fraud.roc_auc is None and no_fraud.pr_auc is None
