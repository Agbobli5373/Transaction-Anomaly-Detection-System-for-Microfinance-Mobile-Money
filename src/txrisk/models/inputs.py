"""The numeric inputs every model sees: features plus the Transaction's own context.

There is one conversion, from a Transactions table and its feature table. Scoring one
Transaction goes through the same conversion on one-row tables, so training and serving can never
disagree about a model's inputs.
"""

from collections.abc import Callable, Sequence
from dataclasses import fields
from datetime import UTC
from operator import attrgetter

import numpy as np
import pandas as pd

from txrisk.features import (
    BANDING_COLUMNS,
    FEATURE_COLUMNS,
    SubjectWalletFeatures,
    Transaction,
    TransactionFeatures,
    features_frame,
)

TRANSACTION_TYPES = ["cash_in", "cash_out", "transfer", "payment", "bill_payment"]
CONTEXT_COLUMNS = [
    "log_amount",
    "hour_of_day",
    "is_app",
    "is_incoming",
    *[f"type_{t}" for t in TRANSACTION_TYPES],
]
# The market spend index sets the High threshold; the models never see it.
MODEL_FEATURE_COLUMNS = [c for c in FEATURE_COLUMNS if c not in BANDING_COLUMNS]
INPUT_COLUMNS = [*MODEL_FEATURE_COLUMNS, *CONTEXT_COLUMNS]


def model_inputs(transactions: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    """One float row per Transaction, indexed by transaction_id, in `transactions` order.

    Booleans become 0 or 1; features that are empty (cold start, no SIM swap) stay NaN, and each
    model decides how to treat them.
    """
    missing = [c for c in MODEL_FEATURE_COLUMNS if c not in features.columns]
    if missing:
        raise ValueError(f"missing feature columns: {missing}")
    rows = transactions[["transaction_id"]].merge(features, on="transaction_id", how="left")
    inputs = pd.DataFrame(index=pd.Index(transactions["transaction_id"], name="transaction_id"))
    for column in MODEL_FEATURE_COLUMNS:
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


def _feature_getters() -> list[Callable[[TransactionFeatures], object]]:
    """How to read each model feature column off a `TransactionFeatures`, in column order."""
    subject = {f.name for f in fields(SubjectWalletFeatures)}
    return [
        attrgetter(f"subject_wallet.{column}" if column in subject else column)
        for column in MODEL_FEATURE_COLUMNS
    ]


_FEATURE_GETTERS = _feature_getters()
_TYPE_COLUMN = {t: i for i, t in enumerate(TRANSACTION_TYPES)}


def input_row(transaction: Transaction, features: TransactionFeatures) -> np.ndarray:
    """The model inputs of one Transaction as a float row in `INPUT_COLUMNS` order, NaN where a
    feature is empty: what `inputs_for` returns, without building a data frame for one row."""
    row = np.full(len(INPUT_COLUMNS), np.nan)
    n = len(_FEATURE_GETTERS)
    for i, get in enumerate(_FEATURE_GETTERS):
        value = get(features)
        if value is not None:
            row[i] = value
    at = transaction.occurred_at
    row[n] = np.log1p(transaction.amount)  # numpy's, as the frame path's, not libm's
    # A time without a zone is read as UTC, as `pd.to_datetime(..., utc=True)` does.
    row[n + 1] = float((at if at.tzinfo is None else at.astimezone(UTC)).hour)
    row[n + 2] = float(transaction.channel == "app")
    row[n + 3] = float(transaction.direction == "in")
    row[n + 4 :] = 0.0
    kind = _TYPE_COLUMN.get(transaction.transaction_type)
    if kind is not None:
        row[n + 4 + kind] = 1.0
    return row


def input_matrix(
    transactions: Sequence[Transaction], features: Sequence[TransactionFeatures]
) -> np.ndarray:
    """`input_row` of each Transaction, one row each."""
    if not transactions:
        return np.empty((0, len(INPUT_COLUMNS)))
    return np.vstack([input_row(t, f) for t, f in zip(transactions, features, strict=True)])
