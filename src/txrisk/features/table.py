"""The feature table for a whole dataset, built by replaying its Transactions in time order."""

import functools
import typing
from dataclasses import fields

import pandas as pd

from txrisk.features.history import ReplayHistory, Transaction
from txrisk.features.subject_wallet import (
    DEFAULT_MIN_HISTORY,
    SubjectWalletFeatures,
    subject_wallet_features,
)

_COUNTERPARTY_COLUMNS = ["counterparty_wallet_id", "agent_id", "merchant_id", "biller_id"]
_DTYPES = {int: "Int64", float: "Float64", bool: "boolean"}


def _transactions(frame: pd.DataFrame) -> list[Transaction]:
    counterparty = functools.reduce(
        lambda found, column: found.fillna(frame[column]),
        _COUNTERPARTY_COLUMNS[1:],
        frame[_COUNTERPARTY_COLUMNS[0]],
    )
    return [
        Transaction(*row)
        for row in zip(
            frame["transaction_id"].tolist(),
            frame["occurred_at"].dt.to_pydatetime().tolist(),
            frame["transaction_type"].tolist(),
            frame["amount"].tolist(),
            frame["subject_wallet_id"].tolist(),
            frame["direction"].tolist(),
            counterparty.tolist(),
            frame["region"].tolist(),
            strict=True,
        )
    ]


def _dtype(annotation: object) -> str:
    """The nullable column type for a feature annotated `T` or `T | None`."""
    types = typing.get_args(annotation) or (annotation,)
    (python_type,) = [t for t in types if t is not type(None)]
    return _DTYPES[python_type]


def feature_table(
    transactions: pd.DataFrame, wallets: pd.DataFrame, *, min_history: int = DEFAULT_MIN_HISTORY
) -> pd.DataFrame:
    """Subject wallet features for every Transaction, one row each in the input's order.

    Each Transaction is scored with the clock at its own occurred-at and only then added to
    the history, so no row can see a Transaction that happened at or after it.
    """
    history = ReplayHistory(
        dict(
            zip(
                wallets["wallet_id"].tolist(),
                wallets["registered_at"].dt.to_pydatetime().tolist(),
                strict=True,
            )
        )
    )
    chronological = transactions.sort_values("occurred_at", kind="stable")
    results: list[SubjectWalletFeatures] = []
    for transaction in _transactions(chronological):
        results.append(
            subject_wallet_features(
                transaction, history, now=transaction.occurred_at, min_history=min_history
            )
        )
        history.record(transaction)

    features = fields(SubjectWalletFeatures)
    table = pd.DataFrame(
        {f.name: [getattr(r, f.name) for r in results] for f in features},
        index=chronological.index,
    ).astype({f.name: _dtype(f.type) for f in features})
    table.insert(0, "transaction_id", chronological["transaction_id"])
    return table.loc[transactions.index].reset_index(drop=True)
