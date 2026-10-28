"""Replay: the live days published at a set pace, and Fraud discoveries becoming Fraud labels
from their event time, through real Redpanda and Postgres."""

import itertools
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from conftest import create_database
from market_helpers import START, Market, build_market
from test_stream import Stream

from txrisk.events import (
    DEFAULT_SECONDS_PER_DAY,
    FraudDiscovered,
    Pacer,
    Publisher,
    ScoringWorker,
    SimSwapRecorded,
    Topics,
    TransactionCreated,
    ensure_topics,
    replay,
    replay_events,
)
from txrisk.events.contracts import Event
from txrisk.events.replay import event_time
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.store import FraudLabels, SimulationClock, create_engine, seed

DAY = timedelta(days=1)
OFFLINE_ONLY = ("ground_truth", "benign_anomalies")
_prefixes = itertools.count(1000)


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The dataset as `txrisk generate` writes it, without Ground truth: replay must not need it."""
    folder = tmp_path_factory.mktemp("generated")
    write_dataset(market.dataset, folder)
    for name in OFFLINE_ONLY:
        (folder / f"{name}.parquet").unlink()
    return folder


def day(n: float) -> datetime:
    return (pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=n)).to_pydatetime()


def busiest_day(market: Market) -> int:
    """A day, from the second week to the end of the Transactions, on which most Fraud
    discoveries are made (they run on for weeks after the last Transaction)."""
    found = market.discoveries["discovered_at"].dt.floor("D")
    in_range = found[(found >= pd.Timestamp(day(8))) & (found < pd.Timestamp(day(20)))]
    return int((in_range.value_counts().idxmax() - pd.Timestamp(START, tz="UTC")).days)


class RecordingPublisher:
    """Stands in for `Publisher`, noting what was published to where and when it was flushed."""

    def __init__(self) -> None:
        self.log: list[tuple[str, str, Event] | str] = []
        self.unconfirmed = 0

    def publish(self, topic: str, key: str, event: Event) -> None:
        self.log.append((topic, key, event))
        self.unconfirmed += 1

    def flush(self) -> None:
        self.log.append("flush")
        self.unconfirmed = 0

    def events(self, topic: str) -> list[Event]:
        return [e[2] for e in self.log if not isinstance(e, str) and e[0] == topic]


def test_replay_events_hold_transactions_sim_swaps_and_discoveries_in_event_time_order(
    market: Market, data: Path
):
    first = busiest_day(market)

    events = list(replay_events(data, since=day(first), until=day(first + 1)))

    times = [event_time(e) for e in events]
    assert times == sorted(times)
    assert all(day(first) <= t < day(first + 1) for t in times)
    kinds = {type(e) for e in events}
    assert {TransactionCreated, FraudDiscovered} <= kinds


def test_the_discoveries_replayed_are_the_datasets_with_their_event_times_unchanged(
    market: Market, data: Path
):
    first = busiest_day(market)
    expected = market.discoveries[
        market.discoveries["discovered_at"].between(
            pd.Timestamp(day(first)), pd.Timestamp(day(first + 1)), inclusive="left"
        )
    ]

    replayed = [
        e
        for e in replay_events(data, since=day(first), until=day(first + 1))
        if isinstance(e, FraudDiscovered)
    ]

    assert {(e.transaction_id, e.discovered_at, e.source) for e in replayed} == {
        (row.transaction_id, row.discovered_at.floor("us").to_pydatetime(), row.source)
        for row in expected.itertuples()
    }
    assert {e.source for e in replayed} <= {"complaint", "chargeback"}


def test_replay_reads_the_discoveries_file_and_never_ground_truth(
    market: Market, data: Path, monkeypatch: pytest.MonkeyPatch
):
    opened: list[str] = []
    read = pd.read_parquet

    def spy(path: Any, *args: Any, **kwargs: Any) -> pd.DataFrame:
        opened.append(Path(path).name)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", spy)

    list(replay_events(data, since=day(14), until=day(16)))

    assert sorted(set(opened)) == [
        "fraud_discoveries.parquet",
        "sim_swaps.parquet",
        "transactions.parquet",
    ]
    assert not any(name in ("ground_truth.parquet", "benign_anomalies.parquet") for name in opened)


def test_the_default_pace_is_one_simulated_day_a_minute():
    assert DEFAULT_SECONDS_PER_DAY == 60


class FakeTime:
    """A wall clock that only moves when the pacer sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def pacer(seconds_per_day: float | None = None) -> tuple[Pacer, FakeTime]:
    wall = FakeTime()
    args = () if seconds_per_day is None else (seconds_per_day,)
    return Pacer(*args, monotonic=wall.monotonic, sleep=wall.sleep), wall


def wait_through(p: Pacer, wall: FakeTime, *moments: datetime) -> list[float]:
    """The waits the pacer asks for before each moment, taking them."""
    waits = []
    for moment in moments:
        wait = p.wait_for(moment)
        if wait > 0:
            p.sleep(wait)
        waits.append(wait)
    return waits


def test_by_default_half_a_simulated_day_takes_thirty_seconds():
    p, wall = pacer()
    start = day(14)

    waits = wait_through(p, wall, start, start + 12 * timedelta(hours=1), start + DAY)

    assert waits == [0.0, pytest.approx(30.0), pytest.approx(30.0)]


def test_speed_is_configurable():
    p, wall = pacer(10)
    start = day(14)

    waits = wait_through(p, wall, start, start + DAY, start + 3 * DAY)

    assert waits == [0.0, pytest.approx(10.0), pytest.approx(20.0)]


def test_a_pace_of_zero_never_waits():
    p, wall = pacer(0)

    assert wait_through(p, wall, day(14), day(15), day(30)) == [0.0, 0.0, 0.0]
    assert wall.slept == []


def test_a_pacer_that_is_behind_catches_up_without_waiting():
    p, wall = pacer(60)
    p.wait_for(day(14))
    wall.now += 90  # publishing was slow: a day and a half of wall time has passed

    assert p.wait_for(day(15)) == 0.0
    assert p.wait_for(day(15) + timedelta(days=0.75)) == pytest.approx(15.0)


def test_a_negative_pace_is_refused():
    with pytest.raises(ValueError, match="negative"):
        Pacer(-1)


def test_replay_publishes_each_kind_to_its_topic_with_event_times_untouched():
    topics = Topics()
    swap = SimSwapRecorded(wallet_id="w-a", occurred_at=day(14))
    found = FraudDiscovered(transaction_id="T1", discovered_at=day(14.5), source="complaint")
    publisher = RecordingPublisher()

    result = replay(publisher, topics, [swap, found], pacer(0)[0])  # type: ignore[arg-type]

    assert publisher.events(topics.sim_swaps) == [swap]
    assert publisher.events(topics.fraud_discoveries) == [found]
    assert (result.sim_swaps, result.fraud_discoveries, result.transactions) == (1, 1, 0)
    # Whatever the speed, the event carries its own time.
    assert publisher.events(topics.fraud_discoveries)[0].discovered_at == day(14.5)  # type: ignore[attr-defined]


def test_replay_waits_for_each_event_and_has_everything_confirmed_before_waiting(
    market: Market, data: Path
):
    first = busiest_day(market)
    events = list(replay_events(data, since=day(first), until=day(first) + timedelta(hours=6)))
    publisher = RecordingPublisher()
    wall = FakeTime()
    unconfirmed_at_sleep: list[int] = []

    def sleep(seconds: float) -> None:
        unconfirmed_at_sleep.append(publisher.unconfirmed)
        wall.sleep(seconds)

    replay(publisher, Topics(), events, Pacer(60, monotonic=wall.monotonic, sleep=sleep))  # type: ignore[arg-type]

    span = event_time(events[-1]) - event_time(events[0])
    assert sum(wall.slept) == pytest.approx(span.total_seconds() / 86_400 * 60, abs=0.05)
    assert unconfirmed_at_sleep and set(unconfirmed_at_sleep) == {0}


def test_replay_stops_after_a_limit_of_transactions(market: Market, data: Path):
    publisher = RecordingPublisher()

    result = replay(
        publisher,
        Topics(),
        replay_events(data, since=day(14), until=day(16)),
        pacer(0)[0],  # type: ignore[arg-type]
        limit=25,
    )

    assert result.transactions == 25


# --- Through Redpanda and Postgres --------------------------------------------------------------


def labelled_worker(stream: Stream) -> ScoringWorker:
    engine = create_engine(stream.database)
    return ScoringWorker(
        stream.scoring(),
        Publisher(stream.bootstrap),
        bootstrap_servers=stream.bootstrap,
        topics=stream.topics,
        group_id=f"{stream.topics.prefix}workers",
        poll_timeout=0.5,
        clock=SimulationClock(engine),
        labels=FraudLabels(engine),
    )


def run_until_handled(worker: ScoringWorker, events: int, *, timeout: float = 180) -> None:
    handled = 0
    deadline = time.monotonic() + timeout
    while handled < events:
        assert time.monotonic() < deadline, f"handled {handled} of {events}"
        handled += worker.run_batch()


@pytest.fixture(scope="module")
def replayed(
    redpanda: str, postgres: str, migrated_template: str, market: Market, data: Path
) -> Iterator[dict[str, Any]]:
    """The market seeded up to its busiest day for discoveries, then that day replayed through
    the stream and consumed by a worker."""
    first = busiest_day(market)
    database = create_database(postgres, "replayed", template=migrated_template)
    engine = create_engine(database)
    seed(engine, data, until=day(first))
    topics = Topics(f"replay{next(_prefixes)}.")
    ensure_topics(redpanda, topics, partitions=3)
    events = list(replay_events(data, since=day(first), until=day(first + 1)))
    published = replay(Publisher(redpanda), topics, iter(events), Pacer(0))
    stream = Stream(redpanda, topics, database)
    worker = labelled_worker(stream)
    run_until_handled(worker, published.transactions + published.fraud_discoveries)
    worker.close()
    yield {
        "stream": stream,
        "events": events,
        "published": published,
        "first": first,
        "engine": engine,
    }


def test_the_replayed_discoveries_become_fraud_labels_dated_by_their_event_time(
    replayed: dict[str, Any],
):
    discoveries = {
        e.transaction_id: e for e in replayed["events"] if isinstance(e, FraudDiscovered)
    }
    # Discoveries before the replay's first day were seeded as labels with the history.
    labels = [
        label
        for label in FraudLabels(replayed["engine"]).known_at(day(replayed["first"] + 2))
        if label.discovered_at >= day(replayed["first"])
    ]

    assert discoveries
    assert {label.transaction_id: (label.discovered_at, label.source) for label in labels} == {
        t: (e.discovered_at, e.source) for t, e in discoveries.items()
    }


def test_a_complaint_or_chargeback_is_not_a_label_before_its_event_time(replayed: dict[str, Any]):
    labels = FraudLabels(replayed["engine"])
    discoveries = sorted(
        (e for e in replayed["events"] if isinstance(e, FraudDiscovered)),
        key=lambda e: e.discovered_at,
    )
    first, last = discoveries[0], discoveries[-1]
    assert first.discovered_at < last.discovered_at

    # Labels seeded with the history (discovered before the replay began) are known from the start.
    before_first = [
        label
        for label in labels.known_at(first.discovered_at - timedelta(microseconds=1))
        if label.discovered_at >= day(replayed["first"])
    ]
    at_first = labels.known_at(first.discovered_at)
    just_before_last = labels.known_at(last.discovered_at - timedelta(microseconds=1))

    # Every label is already stored, and each shows only from its own event time.
    assert before_first == []
    assert first.transaction_id in {label.transaction_id for label in at_first}
    assert last.transaction_id not in {label.transaction_id for label in just_before_last}
    assert last.transaction_id in {
        label.transaction_id for label in labels.known_at(last.discovered_at)
    }


def test_the_clock_ends_at_the_latest_event_time_seen(replayed: dict[str, Any]):
    latest = max(event_time(e) for e in replayed["events"] if not isinstance(e, SimSwapRecorded))

    assert SimulationClock(replayed["engine"]).now() == latest


def test_the_replayed_transactions_are_scored_and_stored(replayed: dict[str, Any]):
    stream: Stream = replayed["stream"]

    assert stream.count("risk_assessments") >= replayed["published"].transactions
    seeded = len(FraudLabels(replayed["engine"]).known_at(day(replayed["first"])))
    assert stream.count("fraud_labels") == seeded + replayed["published"].fraud_discoveries


def test_a_discovery_that_breaks_its_contract_goes_to_the_dead_letter_topic(
    redpanda: str, database: str
):
    topics = Topics(f"replay{next(_prefixes)}.")
    ensure_topics(redpanda, topics, partitions=3)
    stream = Stream(redpanda, topics, database)
    stream.publish_raw(topics.fraud_discoveries, b'{"transaction_id": "T1", "source": "gossip"}')
    worker = labelled_worker(stream)

    run_until_handled(worker, 1)
    worker.close()

    [message] = stream.read(Topics.dead_letter(topics.fraud_discoveries), 1)
    assert dict(message.headers())["topic"] == topics.fraud_discoveries.encode()
    assert stream.count("fraud_labels") == 0


def test_a_repeated_discovery_leaves_one_label(redpanda: str, database: str):
    topics = Topics(f"replay{next(_prefixes)}.")
    ensure_topics(redpanda, topics, partitions=3)
    stream = Stream(redpanda, topics, database)
    found = FraudDiscovered(
        transaction_id="T1", discovered_at=datetime(2026, 2, 1, tzinfo=UTC), source="chargeback"
    )
    publisher = Publisher(redpanda)
    for _ in range(3):
        publisher.publish(topics.fraud_discoveries, found.transaction_id, found)
    publisher.flush()
    worker = labelled_worker(stream)

    run_until_handled(worker, 3)
    worker.close()

    assert stream.count("fraud_labels") == 1
    assert SimulationClock(create_engine(database)).now() == found.discovered_at
