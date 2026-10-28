"""Replaying the live period through the stream: Transactions, SIM swaps and Fraud discoveries,
in event-time order, paced.

Every event keeps its own event time, whatever the pace: speed only decides how long the replay
waits between publishing events (ADR 0005). Fraud discoveries are published when the replay
reaches their event time, so a discovery is never on the stream before the Transactions that
preceded it.

The Fraud discoveries come from `fraud_discoveries.parquet` and nothing else. Ground truth is
never opened (ADR 0007).
"""

import heapq
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from txrisk.events.contracts import FraudDiscovered, SimSwapRecorded, TransactionCreated
from txrisk.events.ledger import in_window, ledger_events
from txrisk.events.stream import Publisher, Topics

ReplayEvent = TransactionCreated | SimSwapRecorded | FraudDiscovered

# One simulated day per minute.
DEFAULT_SECONDS_PER_DAY = 60.0
_SECONDS_PER_DAY = 86_400.0
# Waits shorter than this are skipped: the pace is absolute, so the replay catches up by itself.
_MIN_WAIT = 0.02


def event_time(event: ReplayEvent) -> datetime:
    if isinstance(event, FraudDiscovered):
        return event.discovered_at
    return event.occurred_at


def discovery_events(
    data: Path, *, since: datetime, until: datetime | None = None
) -> Iterator[FraudDiscovered]:
    """The dataset's Fraud discoveries from `since` to before `until`, in event-time order.

    Reads `fraud_discoveries.parquet` only, which is what the operation would learn; the
    generator's Ground truth file is never opened.
    """
    frame = pd.read_parquet(data / "fraud_discoveries.parquet")
    frame = in_window(
        frame.rename(columns={"discovered_at": "occurred_at"}), since, until
    ).sort_values(["occurred_at", "transaction_id"], kind="stable")
    for transaction_id, discovered_at, source in zip(
        frame["transaction_id"].tolist(),
        frame["occurred_at"].dt.to_pydatetime().tolist(),
        frame["source"].tolist(),
        strict=True,
    ):
        yield FraudDiscovered(
            transaction_id=transaction_id, discovered_at=discovered_at, source=source
        )


def replay_events(
    data: Path, *, since: datetime, until: datetime | None = None
) -> Iterator[ReplayEvent]:
    """Transactions, SIM swaps and Fraud discoveries from `since` to before `until`, merged in
    event-time order. At the same instant a discovery comes after the Transactions."""
    return heapq.merge(
        ledger_events(data, since=since, until=until),
        discovery_events(data, since=since, until=until),
        key=event_time,
    )


class Pacer:
    """Maps event time onto wall time: `seconds_per_day` of wall time per simulated day, counted
    from the first event it is asked about. 0 means no waiting at all."""

    def __init__(
        self,
        seconds_per_day: float = DEFAULT_SECONDS_PER_DAY,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if seconds_per_day < 0:
            raise ValueError("seconds_per_day cannot be negative")
        self._seconds_per_day = seconds_per_day
        self._monotonic = monotonic
        self.sleep = sleep
        self._origin: tuple[datetime, float] | None = None

    def wait_for(self, at: datetime) -> float:
        """Seconds to wait before publishing an event of event time `at`; 0 if it is due."""
        if self._origin is None:
            self._origin = (at, self._monotonic())
            return 0.0
        first, started = self._origin
        due = started + (at - first).total_seconds() / _SECONDS_PER_DAY * self._seconds_per_day
        wait = due - self._monotonic()
        return wait if wait >= _MIN_WAIT else 0.0


@dataclass(frozen=True)
class Replayed:
    transactions: int
    sim_swaps: int
    fraud_discoveries: int


def replay(
    publisher: Publisher,
    topics: Topics,
    events: Iterable[ReplayEvent],
    pacer: Pacer,
    *,
    limit: int | None = None,
) -> Replayed:
    """Publish the events in order, each when the pacer says it is due. Stops after `limit`
    Transactions, if given.

    Everything queued is confirmed by the broker before each wait, so a paced event is on the
    stream when it is due. As in `publish_ledger`, a SIM swap is confirmed before any later
    Transaction is published, and Transactions are keyed by Subject wallet.
    """
    transactions = sim_swaps = discoveries = 0
    for event in events:
        if isinstance(event, TransactionCreated) and limit is not None and transactions >= limit:
            break
        wait = pacer.wait_for(event_time(event))
        if wait > 0:
            publisher.flush()
            pacer.sleep(wait)
        if isinstance(event, SimSwapRecorded):
            publisher.publish(topics.sim_swaps, event.wallet_id, event)
            publisher.flush()
            sim_swaps += 1
        elif isinstance(event, FraudDiscovered):
            publisher.publish(topics.fraud_discoveries, event.transaction_id, event)
            discoveries += 1
        else:
            publisher.publish(topics.transactions, event.subject_wallet_id, event)
            transactions += 1
    publisher.flush()
    return Replayed(transactions=transactions, sim_swaps=sim_swaps, fraud_discoveries=discoveries)
