"""The Anomaly score: how unusual a Transaction looks, as a percentile of normal behaviour."""

import numpy as np
import pandas as pd

from txrisk.models import INPUT_COLUMNS
from txrisk.models.anomaly import AnomalyModel


def _ordinary(rows: int, seed: int = 0) -> pd.DataFrame:
    """Everyday inputs: values clustered around typical levels, some features empty."""
    rng = np.random.default_rng(seed)
    inputs = pd.DataFrame(
        rng.normal(1.0, 0.1, (rows, len(INPUT_COLUMNS))),
        columns=INPUT_COLUMNS,
        index=pd.Index([f"T{i}" for i in range(rows)], name="transaction_id"),
    )
    inputs.loc[inputs.index[::7], "seconds_since_sim_swap"] = np.nan
    return inputs


def test_the_anomaly_score_is_a_percentile_of_the_training_data():
    training = _ordinary(2000)
    model = AnomalyModel.fit(training, seed=1)

    scores = model.score(training)

    assert scores.between(0, 1).all()
    # Training data is, by definition, spread evenly over its own percentiles.
    assert abs(scores.median() - 0.5) < 0.05


def test_something_far_from_normal_scores_near_the_top():
    model = AnomalyModel.fit(_ordinary(2000), seed=1)
    odd = _ordinary(1, seed=9)
    odd[["share_of_balance_moved", "receiver_fan_in_24h", "log_amount"]] = [9.0, 40.0, 12.0]

    assert model.score(odd).iloc[0] > 0.99
    assert model.score(_ordinary(200, seed=3)).median() < 0.8


def test_the_same_seed_gives_the_same_scores():
    training, later = _ordinary(1000), _ordinary(50, seed=5)

    first = AnomalyModel.fit(training, seed=7).score(later)
    second = AnomalyModel.fit(training, seed=7).score(later)

    pd.testing.assert_series_equal(first, second)


def test_scores_keep_the_transactions_they_belong_to():
    model = AnomalyModel.fit(_ordinary(500), seed=1)
    later = _ordinary(5, seed=2)

    assert model.score(later).index.equals(later.index)


def test_lof_judges_distance_regardless_of_the_units_a_feature_is_measured_in():
    training, later = _ordinary(800), _ordinary(50, seed=5)
    in_seconds = training.assign(
        seconds_since_last_transaction=training["seconds_since_last_transaction"] * 1e6
    )
    later_in_seconds = later.assign(
        seconds_since_last_transaction=later["seconds_since_last_transaction"] * 1e6
    )

    scores = AnomalyModel.fit_lof(training, seed=1).score(later)
    rescaled = AnomalyModel.fit_lof(in_seconds, seed=1).score(later_in_seconds)

    pd.testing.assert_series_equal(scores, rescaled)


def test_input_contributions_add_up_to_the_rise_above_a_typical_transaction():
    model = AnomalyModel.fit(_ordinary(2000), seed=1)
    later = _ordinary(30, seed=4)

    contributions = model.contributions(later)

    assert list(contributions.columns) == INPUT_COLUMNS
    assert contributions.index.equals(later.index)
    # What the contributions leave unexplained is the score of a typical Transaction: one level.
    typical = model.score(later) - contributions.sum(axis=1)
    assert np.ptp(typical.to_numpy()) < 1e-9


def test_the_input_that_makes_a_transaction_unusual_contributes_most():
    model = AnomalyModel.fit(_ordinary(2000), seed=1)
    # Typical in every other way: a one-row frame would otherwise have no SIM swap, rare here.
    odd = _ordinary(1, seed=9).assign(share_of_balance_moved=9.0, seconds_since_sim_swap=1.0)

    contributions = model.contributions(odd).iloc[0]

    assert contributions.idxmax() == "share_of_balance_moved"
    assert contributions.max() > 0
