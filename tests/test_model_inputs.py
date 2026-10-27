"""The numeric inputs every model sees, the same whether built for training or for one score."""

import math

import numpy as np
import pandas as pd
import pytest
from scoring_helpers import FEATURES, NOW, Market, routine, tx

from txrisk.features import feature_table, transaction_features
from txrisk.models import FEATURE_COLUMNS, INPUT_COLUMNS, inputs_for, model_inputs

TRANSACTION_COLUMNS = {
    "transaction_id": "T1",
    "occurred_at": pd.Timestamp("2030-06-01 03:30", tz="UTC"),
    "transaction_type": "cash_out",
    "amount": 99.0,
    "direction": "out",
    "channel": "app",
}


def test_inputs_are_numeric_with_missing_values_left_missing():
    transactions = pd.DataFrame([TRANSACTION_COLUMNS])
    features = pd.DataFrame(
        [
            {
                "transaction_id": "T1",
                **dict.fromkeys(FEATURE_COLUMNS, pd.NA),
                "history_count": 7,
                "new_counterparty": True,
                "amount_to_kyc_limit": 0.099,
            }
        ]
    ).astype({"new_counterparty": "boolean", "seconds_since_sim_swap": "Float64"})

    inputs = model_inputs(transactions, features)

    row = inputs.loc["T1"]
    assert (inputs.dtypes == np.float64).all()
    assert row["history_count"] == 7.0
    assert row["new_counterparty"] == 1.0
    assert math.isnan(row["seconds_since_sim_swap"])
    assert row["log_amount"] == math.log1p(99.0)
    assert row["hour_of_day"] == 3.0
    assert row["is_app"] == 1.0
    assert row["is_incoming"] == 0.0
    assert row["type_cash_out"] == 1.0 and row["type_transfer"] == 0.0


def test_every_model_input_column_is_present_in_a_fixed_order():
    market = Market().record(*routine())
    scored = tx(NOW, transaction_type="payment", amount=120.0)
    features = transaction_features(scored, market.history, now=NOW, config=FEATURES)

    assert list(inputs_for(scored, features).columns) == INPUT_COLUMNS


def test_one_scored_transaction_gets_exactly_the_inputs_training_would_give_it():
    past = routine()
    scored = tx(NOW, transaction_type="cash_out", amount=640.0, counterparty="A1")
    frame = pd.DataFrame(
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
                "agent_id": t.counterparty_id if t.transaction_type == "cash_out" else None,
                "merchant_id": None,
                "biller_id": None,
            }
            for t in [*past, scored]
        ]
    )
    wallets = pd.DataFrame(
        {
            "wallet_id": ["w-subject", "w-friend"],
            "registered_at": pd.to_datetime(["2030-01-01"] * 2, utc=True),
            "kyc_tier": ["minimum", "minimum"],
        }
    )
    no_sim_swaps = pd.DataFrame(
        {"wallet_id": pd.Series(dtype=str), "occurred_at": pd.Series(dtype="datetime64[us, UTC]")}
    )

    training = model_inputs(frame, feature_table(frame, wallets, no_sim_swaps, config=FEATURES))
    market = Market().record(*past)
    serving = inputs_for(
        scored, transaction_features(scored, market.history, now=NOW, config=FEATURES)
    )

    pd.testing.assert_frame_equal(serving, training.loc[[scored.transaction_id]])


def test_a_feature_table_missing_a_column_is_refused():
    transactions = pd.DataFrame([TRANSACTION_COLUMNS])
    features = pd.DataFrame([{"transaction_id": "T1", "history_count": 7}])

    with pytest.raises(ValueError, match="missing feature columns"):
        model_inputs(transactions, features)
