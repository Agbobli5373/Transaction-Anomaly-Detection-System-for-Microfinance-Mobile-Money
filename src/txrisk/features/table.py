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


def transactions_from_frame(frame: pd.DataFrame) -> list[Transaction]:
    """The rows of a Transactions table as the features see them, in the frame's order."""
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
            frame["device_id"].tolist(),
            frame["channel"].tolist(),
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


# Every feature column, in the order the feature table and the models use.
FEATURE_COLUMNS = [f.name for f in fields(SubjectWalletFeatures)] + [
    f.name for f in fields(TransactionFeatures) if f.name != "subject_wallet"
]


# Features that are not model inputs: the models never see them (see models/inputs.py).
BANDING_COLUMNS = ["market_spend_index"]


def features_frame(transaction_ids: list[str], results: list[TransactionFeatures]) -> pd.DataFrame:
    """Feature table rows for these Transactions: transaction_id, then every feature column."""
    columns = _columns(results)
    table = pd.DataFrame({name: values for name, (values, _) in columns.items()}).astype(
        {name: dtype for name, (_, dtype) in columns.items()}
    )
    table.insert(0, "transaction_id", pd.Series(transaction_ids, dtype="string"))
    return table


def _plain(value: object) -> object:
    """A table cell as the feature dataclasses hold it: None when empty, else a Python value."""
    if value is pd.NA or value is None or (isinstance(value, float) and value != value):
        return None
    return value.item() if hasattr(value, "item") else value


def features_from_frame(table: pd.DataFrame) -> list[TransactionFeatures]:
    """The features of each feature table row, as the feature function returned them."""
    subject_fields = [f.name for f in fields(SubjectWalletFeatures)]
    other_fields = [f.name for f in fields(TransactionFeatures) if f.name != "subject_wallet"]
    results: list[TransactionFeatures] = []
    for row in table[subject_fields + other_fields].itertuples(index=False):
        values = dict(zip(subject_fields + other_fields, map(_plain, row), strict=True))
        subject = SubjectWalletFeatures(**{name: values[name] for name in subject_fields})  # type: ignore[arg-type]
        results.append(
            TransactionFeatures(
                subject_wallet=subject,
                **{name: values[name] for name in other_fields},  # type: ignore[arg-type]
            )
        )
    return results


def replay_history(wallets: pd.DataFrame, sim_swaps: pd.DataFrame) -> ReplayHistory:
    """An empty replay over these Wallets, with every SIM swap known up front.

    The history only ever answers with SIM swaps strictly before the clock, so knowing them all
    in advance leaks nothing; Transactions are recorded as the replay reaches them.
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
    return history


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
    history = replay_history(wallets, sim_swaps)
    chronological = transactions.sort_values("occurred_at", kind="stable")
    results: list[TransactionFeatures] = []
    for transaction in transactions_from_frame(chronological):
        results.append(
            transaction_features(transaction, history, now=transaction.occurred_at, config=config)
        )
        history.record(transaction)

    table = features_frame(chronological["transaction_id"].tolist(), results)
    table.index = chronological.index
    return table.loc[transactions.index].reset_index(drop=True)
