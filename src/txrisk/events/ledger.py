"""Standing in for the ledger: publishing a generated dataset's Transactions and SIM swaps.

Only what the operator's systems would publish is read. Ground truth and Benign anomalies are never
opened (ADR 0007), and the Fraud discoveries are published only by `events.replay`.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from txrisk.events.contracts import SimSwapRecorded, TransactionCreated
from txrisk.events.stream import Publisher, Topics
from txrisk.features import transactions_from_frame

_TRANSACTION_COLUMNS = [
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


@dataclass(frozen=True)
class Published:
    transactions: int
    sim_swaps: int


def in_window(frame: pd.DataFrame, since: datetime, until: datetime | None) -> pd.DataFrame:
    # Postgres keeps microseconds, and the seeded history is cut to them too.
    frame = frame.assign(occurred_at=frame["occurred_at"].dt.floor("us"))
    keep = frame["occurred_at"] >= since
    if until is not None:
        keep &= frame["occurred_at"] < until
    return frame[keep]


def ledger_events(
    data: Path, *, since: datetime, until: datetime | None = None
) -> Iterator[TransactionCreated | SimSwapRecorded]:
    """The dataset's SIM swaps and Transactions in [`since`, `until`), in event-time order.

    Ties keep the order the history is read in: by transaction ID, and a SIM swap before a
    Transaction at the same moment (which it does not affect: features see only what came
    strictly before).
    """
    transactions = in_window(
        pd.read_parquet(data / "transactions.parquet", columns=_TRANSACTION_COLUMNS), since, until
    ).sort_values(["occurred_at", "transaction_id"], kind="stable")
    swaps = in_window(pd.read_parquet(data / "sim_swaps.parquet"), since, until).sort_values(
        ["occurred_at", "wallet_id"], kind="stable"
    )
    swap_events = [
        SimSwapRecorded(wallet_id=wallet_id, occurred_at=occurred_at)
        for wallet_id, occurred_at in zip(
            swaps["wallet_id"].tolist(),
            swaps["occurred_at"].dt.to_pydatetime().tolist(),
            strict=True,
        )
    ]
    pending = iter(swap_events)
    swap = next(pending, None)
    for transaction in transactions_from_frame(transactions):
        while swap is not None and swap.occurred_at <= transaction.occurred_at:
            yield swap
            swap = next(pending, None)
        yield TransactionCreated.of(transaction)
    while swap is not None:
        yield swap
        swap = next(pending, None)


def publish_ledger(
    publisher: Publisher,
    topics: Topics,
    events: Iterator[TransactionCreated | SimSwapRecorded],
    *,
    limit: int | None = None,
) -> Published:
    """Publish the events in order: Transactions keyed by Subject wallet, so each Subject
    wallet's Transactions are consumed in order, and SIM swaps keyed by Wallet.

    A SIM swap is confirmed by the broker before any later Transaction is published, so a worker
    that has consumed a Transaction can rely on every earlier SIM swap being on its topic.
    Stops after `limit` Transactions, if given.
    """
    transactions = sim_swaps = 0
    for event in events:
        if isinstance(event, SimSwapRecorded):
            publisher.publish(topics.sim_swaps, event.wallet_id, event)
            publisher.flush()
            sim_swaps += 1
            continue
        if limit is not None and transactions >= limit:
            break
        publisher.publish(topics.transactions, event.subject_wallet_id, event)
        transactions += 1
    publisher.flush()
    return Published(transactions=transactions, sim_swaps=sim_swaps)
