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
    subject_balance_before: float


@dataclass(frozen=True)
class Wallet:
    registered_at: datetime
    kyc_tier: str


@dataclass(frozen=True)
class SubjectHistory:
    """What is known about a Subject wallet at one moment."""

    registered_at: datetime
    # The Wallet's Transactions as Subject wallet, oldest first.
    transactions: Sequence[Transaction]


class HistoryProvider(Protocol):
    """Every call answers as of one moment and sees nothing at or after `before`."""

    def wallet(self, wallet_id: str) -> Wallet:
        """The Wallet's registration and KYC tier."""
        ...

    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        """The Wallet's Transactions as Subject wallet that occurred strictly before `before`."""
        ...

    def incoming_transfers(
        self, wallet_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        """Transfers into the Wallet that occurred in [`since`, `before`), oldest first."""
        ...

    def agent_transactions(
        self, agent_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        """Cash-ins and Cash-outs at the Agent in [`since`, `before`), oldest first."""
        ...

    def last_sim_swap(self, wallet_id: str, before: datetime) -> datetime | None:
        """When the Wallet's latest SIM swap strictly before `before` occurred, if it had one."""
        ...


class _Timeline:
    """Transactions kept in time order, read back by time range."""

    def __init__(self) -> None:
        self._times: list[datetime] = []
        self._transactions: list[Transaction] = []

    def add(self, transaction: Transaction) -> None:
        # Replay records in time order, so this is an append in practice.
        i = bisect.bisect_right(self._times, transaction.occurred_at)
        self._times.insert(i, transaction.occurred_at)
        self._transactions.insert(i, transaction)

    def between(self, since: datetime | None, before: datetime) -> Sequence[Transaction]:
        start = 0 if since is None else bisect.bisect_left(self._times, since)
        return self._transactions[start : bisect.bisect_left(self._times, before)]


_EMPTY = _Timeline()
# The Transaction types whose Counterparty is an Agent.
_AGENT_TYPES = frozenset({"cash_in", "cash_out"})


class ReplayHistory:
    """Offline history built by recording Transactions as a chronological replay reaches them."""

    def __init__(self, wallets: Mapping[str, Wallet]) -> None:
        self._wallets = wallets
        self._by_subject: dict[str, _Timeline] = {}
        self._by_receiver: dict[str, _Timeline] = {}
        self._by_agent: dict[str, _Timeline] = {}
        self._sim_swaps: dict[str, list[datetime]] = {}

    def record(self, transaction: Transaction) -> None:
        self._by_subject.setdefault(transaction.subject_wallet_id, _Timeline()).add(transaction)
        if transaction.transaction_type == "transfer":
            self._by_receiver.setdefault(transaction.counterparty_id, _Timeline()).add(transaction)
        elif transaction.transaction_type in _AGENT_TYPES:
            self._by_agent.setdefault(transaction.counterparty_id, _Timeline()).add(transaction)

    def record_sim_swap(self, wallet_id: str, occurred_at: datetime) -> None:
        bisect.insort(self._sim_swaps.setdefault(wallet_id, []), occurred_at)

    def wallet(self, wallet_id: str) -> Wallet:
        return self._wallets[wallet_id]

    def subject_history(self, wallet_id: str, before: datetime) -> SubjectHistory:
        return SubjectHistory(
            registered_at=self._wallets[wallet_id].registered_at,
            transactions=self._by_subject.get(wallet_id, _EMPTY).between(None, before),
        )

    def incoming_transfers(
        self, wallet_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        return self._by_receiver.get(wallet_id, _EMPTY).between(since, before)

    def agent_transactions(
        self, agent_id: str, since: datetime, before: datetime
    ) -> Sequence[Transaction]:
        return self._by_agent.get(agent_id, _EMPTY).between(since, before)

    def last_sim_swap(self, wallet_id: str, before: datetime) -> datetime | None:
        swaps = self._sim_swaps.get(wallet_id, [])
        i = bisect.bisect_left(swaps, before)
        return swaps[i - 1] if i else None
