"""What the load tests send: the dataset's own Transactions, each under a fresh transaction ID.

A run draws from the last Transactions of a generated dataset in event-time order, so the Wallets,
Agents, Merchants and Billers are the ones `seed` registered and the Transactions look like the
live traffic. Each is sent under a new ID, since a repeated ID is answered from storage and would
measure the idempotent replay, not scoring. The IDs carry a per-run prefix, so a second run
against the same database scores afresh. Ground truth is never read (ADR 0007).
"""

import itertools
from pathlib import Path

import pandas as pd

from txrisk.events.contracts import TransactionCreated
from txrisk.features import transactions_from_frame

_COLUMNS = [
    "transaction_id",
    "occurred_at",
    "transaction_type",
    "amount",
    "subject_wallet_id",
    "direction",
    "subject_balance_before",
    "channel",
    "device_id",
    "region",
    "counterparty_wallet_id",
    "agent_id",
    "merchant_id",
    "biller_id",
]


def load_pool(data: Path, *, size: int) -> list[TransactionCreated]:
    """The last `size` Transactions of the dataset, in event-time order."""
    frame = pd.read_parquet(data / "transactions.parquet", columns=_COLUMNS)
    # Postgres keeps microseconds, as in the ledger events.
    frame = frame.assign(occurred_at=frame["occurred_at"].dt.floor("us"))
    frame = frame.sort_values(["occurred_at", "transaction_id"], kind="stable").tail(size)
    return [TransactionCreated.of(t) for t in transactions_from_frame(frame)]


class Workload:
    """An endless sequence of Transactions, each with an ID no earlier one had.

    The pool is cycled in order, so a Wallet's Transactions keep their order within a pass.
    """

    def __init__(self, pool: list[TransactionCreated], *, run_id: str) -> None:
        if not pool:
            raise ValueError("the pool has no Transactions to send")
        self._pool = pool
        self._run_id = run_id
        self._count = itertools.count()

    def next(self) -> TransactionCreated:
        n = next(self._count)
        return self._pool[n % len(self._pool)].model_copy(
            update={"transaction_id": f"load-{self._run_id}-{n}"}
        )
