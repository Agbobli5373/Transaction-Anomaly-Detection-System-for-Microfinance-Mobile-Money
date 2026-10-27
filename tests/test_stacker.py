"""The stacker: one Risk score from the rules, the Anomaly score and the supervised probability."""

import math

import numpy as np
import pandas as pd
import pytest
from scoring_helpers import FEATURES, NOW, Market, routine, tx

from txrisk.features import feature_table, features_from_frame, transaction_features
from txrisk.models.stacked import STACK_COLUMNS, Stacker, stack_components


def _components(rows: int, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Fraud raises the supervised probability strongly and the Anomaly score a little; the rules
    score here is noise."""
    rng = np.random.default_rng(seed)
    fraud = pd.Series(rng.random(rows) < 0.05)
    components = stack_components(
        rules_score=pd.Series(rng.random(rows) * 0.3),
        anomaly_score=pd.Series(np.clip(rng.normal(0.5 + 0.2 * fraud, 0.2), 0, 1)),
        raw_probability=pd.Series(np.clip(rng.beta(1, 20, rows) + 0.6 * fraud, 0, 0.999)),
    )
    return components, fraud


def test_components_are_put_on_the_scales_the_stacker_learns_from():
    components = stack_components(
        rules_score=pd.Series([0.25]),
        anomaly_score=pd.Series([0.9]),
        raw_probability=pd.Series([0.5]),
    )

    assert list(components.columns) == STACK_COLUMNS
    assert components.iloc[0].tolist() == [0.25, 0.9, 0.0]  # log-odds of 0.5 is 0


def test_the_stacker_weighs_what_predicts_fraud():
    components, fraud = _components(20_000)

    stacker = Stacker.fit(components, fraud)

    assert stacker.weights["supervised_log_odds"] > stacker.weights["anomaly"] > 0
    assert abs(stacker.weights["rules"]) < stacker.weights["anomaly"]


def test_the_risk_score_is_a_0_to_100_score_that_rises_with_each_useful_component():
    components, fraud = _components(20_000)
    stacker = Stacker.fit(components, fraud)
    base = components.iloc[[0]]

    scores = stacker.score(components)
    higher = stacker.score(base.assign(supervised_log_odds=base["supervised_log_odds"] + 2))

    assert scores.between(0, 100).all()
    assert higher.iloc[0] > stacker.score(base).iloc[0]


def test_the_stacker_is_the_same_every_time():
    components, fraud = _components(5_000)

    assert Stacker.fit(components, fraud) == Stacker.fit(components, fraud)


def test_extreme_probabilities_do_not_break_the_log_odds():
    components = stack_components(
        rules_score=pd.Series([0.0, 0.0]),
        anomaly_score=pd.Series([0.0, 1.0]),
        raw_probability=pd.Series([0.0, 1.0]),
    )

    assert components["supervised_log_odds"].map(math.isfinite).all()


def test_features_rebuilt_from_a_feature_table_row_are_the_ones_computed():
    past = routine()
    scored = tx(NOW, transaction_type="payment", amount=300.0, region="Volta")
    market = Market().record(*past)
    computed = transaction_features(scored, market.history, now=NOW, config=FEATURES)
    frame = _frame([*past, scored])
    wallets = pd.DataFrame(
        {
            "wallet_id": ["w-subject", "w-friend"],
            "registered_at": pd.to_datetime(["2030-01-01"] * 2, utc=True),
            "kyc_tier": ["minimum", "minimum"],
        }
    )
    no_swaps = pd.DataFrame(
        {"wallet_id": pd.Series(dtype=str), "occurred_at": pd.Series(dtype="datetime64[us, UTC]")}
    )

    table = feature_table(frame, wallets, no_swaps, config=FEATURES)
    rebuilt = features_from_frame(table)

    assert rebuilt[-1] == computed


def _frame(transactions: list) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "transaction_id": t.transaction_id,
                "occurred_at": pd.Timestamp(t.occurred_at),
                "transaction_type": t.transaction_type,
                "amount": t.amount,
                "subject_wallet_id": t.subject_wallet_id,
                "direction": t.direction,
                "subject_balance_before": t.subject_balance_before,
                "channel": t.channel,
                "device_id": t.device_id,
                "region": t.region,
                "counterparty_wallet_id": t.counterparty_id
                if t.transaction_type == "transfer"
                else None,
                "agent_id": None,
                "merchant_id": t.counterparty_id if t.transaction_type == "payment" else None,
                "biller_id": None,
            }
            for t in transactions
        ]
    )


@pytest.mark.parametrize("probability", [1e-12, 0.5, 1 - 1e-12])
def test_log_odds_round_trip(probability: float):
    components = stack_components(
        rules_score=pd.Series([0.0]),
        anomaly_score=pd.Series([0.0]),
        raw_probability=pd.Series([probability]),
    )
    log_odds = components["supervised_log_odds"].iloc[0]

    assert 1 / (1 + math.exp(-log_odds)) == pytest.approx(min(max(probability, 1e-6), 1 - 1e-6))
