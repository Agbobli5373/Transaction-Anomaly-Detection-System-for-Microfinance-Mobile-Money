"""The numeric inputs every model sees: features plus the Transaction's own context.

There is one conversion, from a Transactions table and its feature table. Scoring one
Transaction goes through the same conversion on one-row tables, so training and serving can never
disagree about a model's inputs.
"""

import numpy as np
import pandas as pd

from txrisk.features import FEATURE_COLUMNS, Transaction, TransactionFeatures, features_frame

TRANSACTION_TYPES = ["cash_in", "cash_out", "transfer", "payment", "bill_payment"]
CONTEXT_COLUMNS = [
    "log_amount",
    "hour_of_day",
    "is_app",
    "is_incoming",
    *[f"type_{t}" for t in TRANSACTION_TYPES],
]
INPUT_COLUMNS = [*FEATURE_COLUMNS, *CONTEXT_COLUMNS]


def model_inputs(transactions: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """One float row per Transaction, indexed by transaction_id, in `transactions` order.

    Booleans become 0 or 1; features that are empty (cold start, no SIM swap) stay NaN, and each
    model decides how to treat them.
    """
    missing = [c for c in FEATURE_COLUMNS if c not in features.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")
    rows = transactions[["transaction_id"]].merge(features, on="transaction_id", how="left")
    inputs = pd.DataFrame(index=pd.Index(transactions["transaction_id"], name="transaction_id"))
    for column in FEATURE_COLUMNS:
        inputs[column] = rows[column].astype("Float64").to_numpy(dtype=np.float64, na_value=np.nan)
    inputs["log_amount"] = np.log1p(transactions["amount"].to_numpy(dtype=np.float64))
    inputs["hour_of_day"] = transactions["occurred_at"].dt.hour.to_numpy(dtype=np.float64)
    inputs["is_app"] = (transactions["channel"] == "app").to_numpy(dtype=np.float64)
    inputs["is_incoming"] = (transactions["direction"] == "in").to_numpy(dtype=np.float64)
    for t in TRANSACTION_TYPES:
        inputs[f"type_{t}"] = (transactions["transaction_type"] == t).to_numpy(dtype=np.float64)
    return inputs


def inputs_for(transaction: Transaction, features: TransactionFeatures) -> pd.DataFrame:
    """The one-row model inputs for a Transaction being scored."""
    row = pd.DataFrame(
        {
            "transaction_id": [transaction.transaction_id],
            "occurred_at": pd.to_datetime([transaction.occurred_at], utc=True),
            "transaction_type": [transaction.transaction_type],
            "amount": [transaction.amount],
            "direction": [transaction.direction],
            "channel": [transaction.channel],
        }
    )
    return model_inputs(row, features_frame([transaction.transaction_id], [features]))
