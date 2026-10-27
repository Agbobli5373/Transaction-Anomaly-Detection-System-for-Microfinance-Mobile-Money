"""The feature table for a whole dataset, built by replaying its Transactions in time order."""

import functools
import typing
from dataclasses import fields

import pandas as pd

from txrisk.features.config import FeatureConfig
from txrisk.features.history import ReplayHistory, Transaction, Wallet
from txrisk.features.subject_wallet import SubjectWalletFeatures
from txrisk.features.transaction import TransactionFeatures, transaction_features

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
            frame["subject_balance_before"].tolist(),
            strict=True,
        )
    ]


def _dtype(annotation: object) -> str:
    """The nullable column type for a feature annotated `T` or `T | None`."""
    types = typing.get_args(annotation) or (annotation,)
    (python_type,) = [t for t in types if t is not type(None)]
    return _DTYPES[python_type]


def _columns(results: list[TransactionFeatures]) -> dict[str, tuple[list[object], str]]:
    """Each feature's values and nullable column type, Subject wallet features first."""
    subject = [r.subject_wallet for r in results]
    columns = {
        f.name: ([getattr(r, f.name) for r in subject], _dtype(f.type))
        for f in fields(SubjectWalletFeatures)
    }
    for f in fields(TransactionFeatures):
        if f.name != "subject_wallet":
            columns[f.name] = ([getattr(r, f.name) for r in results], _dtype(f.type))
    return columns


def feature_table(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    config: FeatureConfig,
) -> pd.DataFrame:
    """Every feature of every Transaction, one row each in the input's order.

    Each Transaction is scored with the clock at its own occurred-at and only then added to
    the history, so no row can see a Transaction that happened at or after it. SIM swaps are
    all known up front; the history only ever answers with those strictly before the clock.
    """
    history = ReplayHistory(
        {
            wallet_id: Wallet(registered_at=registered_at, kyc_tier=kyc_tier)
            for wallet_id, registered_at, kyc_tier in zip(
                wallets["wallet_id"].tolist(),
                wallets["registered_at"].dt.to_pydatetime().tolist(),
                wallets["kyc_tier"].tolist(),
                strict=True,
            )
        }
    )
    for wallet_id, occurred_at in zip(
        sim_swaps["wallet_id"].tolist(),
        sim_swaps["occurred_at"].dt.to_pydatetime().tolist(),
        strict=True,
    ):
        history.record_sim_swap(wallet_id, occurred_at)
    chronological = transactions.sort_values("occurred_at", kind="stable")
    results: list[TransactionFeatures] = []
    for transaction in _transactions(chronological):
        results.append(
            transaction_features(transaction, history, now=transaction.occurred_at, config=config)
        )
        history.record(transaction)

    columns = _columns(results)
    table = pd.DataFrame(
        {name: values for name, (values, _) in columns.items()}, index=chronological.index
    ).astype({name: dtype for name, (_, dtype) in columns.items()})
    table.insert(0, "transaction_id", chronological["transaction_id"])
    return table.loc[transactions.index].reset_index(drop=True)
