"""Transactions as the features see them, and the history they are read from."""

import bisect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    occurred_at: datetime
    transaction_type: str
    amount: float
    subject_wallet_id: str
    direction: str
    counterparty_id: str
    region: str


@dataclass(frozen=True)
class SubjectHistory:
    """What is known about a Subject wallet at one moment."""

    registered_at: datetime
    # The Wallet's Transactions as Subject wallet, oldest first.
    transactions: Sequence[Transaction]


class HistoryProvider(Protocol):
    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        """The Wallet's Transactions as Subject wallet that occurred strictly before `before`."""
        ...


class ReplayHistory:
    """Offline history built by recording Transactions as a chronological replay reaches them."""

    def __init__(self, registrations: Mapping[str, datetime]) -> None:
        self._registrations = registrations
        self._times: dict[str, list[datetime]] = {}
        self._transactions: dict[str, list[Transaction]] = {}

    def record(self, transaction: Transaction) -> None:
        wallet = transaction.subject_wallet_id
        times = self._times.setdefault(wallet, [])
        transactions = self._transactions.setdefault(wallet, [])
        # Replay records in time order, so this is an append in practice.
        i = bisect.bisect_right(times, transaction.occurred_at)
        times.insert(i, transaction.occurred_at)
        transactions.insert(i, transaction)

    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        times = self._times.get(wallet_id, [])
        end = bisect.bisect_left(times, before)
        return SubjectHistory(
            registered_at=self._registrations[wallet_id],
            transactions=self._transactions.get(wallet_id, [])[:end],
        )
