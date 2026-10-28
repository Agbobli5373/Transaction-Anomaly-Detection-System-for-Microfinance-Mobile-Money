"""The stream path against real Redpanda and Postgres: Transactions in on `transaction.created`,
Risk assessments out on `score.produced`, each Transaction scored exactly once."""

import dataclasses
import itertools
import json
import time
from collections import defaultdict
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
import sqlalchemy as sa
from api_helpers import as_app_role, settings
from confluent_kafka import Consumer
from scoring_helpers import DAY, HOUR, NOW, REGISTERED, SUBJECT, routine, tx

from txrisk.events import (
    Publisher,
    ScoreProduced,
    ScoringWorker,
    SimSwapRecorded,
    Topics,
    TransactionCreated,
    decode,
    encode,
    ensure_topics,
)
from txrisk.features import Transaction
from txrisk.store import Scored, StoredScoring, create_engine, records

FRIEND = "w-friend"
STRANGER = "w-stranger"
AGENT = "A00001"

_prefixes = itertools.count()


def on_app(transaction: Transaction) -> Transaction:
    return dataclasses.replace(transaction, channel="app")


def drain(at: datetime = NOW) -> Transaction:
    """A Transfer of 90% of the balance to a stranger."""
    return on_app(tx(at, amount=900.0, counterparty=STRANGER, balance=1000.0))


@dataclasses.dataclass
class Stream:
    """One test's topics on the shared broker, and the database its workers score into."""

    bootstrap: str
    topics: Topics
    database: str

    def scoring(self) -> StoredScoring:
        api = settings(self.database)
        return StoredScoring(
            create_engine(as_app_role(self.database)), features=api.features(), rules=api.rules()
        )

    def worker(self, scoring: Any = None, *, group: str = "workers") -> ScoringWorker:
        return ScoringWorker(
            scoring or self.scoring(),
            Publisher(self.bootstrap),
            bootstrap_servers=self.bootstrap,
            topics=self.topics,
            group_id=f"{self.topics.prefix}{group}",
            poll_timeout=0.5,
        )

    def publish(self, *events: TransactionCreated | SimSwapRecorded) -> None:
        publisher = Publisher(self.bootstrap)
        for event in events:
            if isinstance(event, SimSwapRecorded):
                publisher.publish(self.topics.sim_swaps, event.wallet_id, event)
            else:
                publisher.publish(self.topics.transactions, event.subject_wallet_id, event)
            publisher.flush()

    def publish_raw(self, topic: str, value: bytes) -> None:
        publisher = Publisher(self.bootstrap)
        publisher.publish_raw(topic, b"key", value)
        publisher.flush()

    def read(self, topic: str, count: int, *, timeout: float = 60) -> list[Any]:
        """Every message on the topic, read by a fresh consumer once at least `count` are
        there and no more arrive for a couple of seconds."""
        consumer = Consumer(
            {
                "bootstrap.servers": self.bootstrap,
                "group.id": f"reader-{next(_prefixes)}",
                "auto.offset.reset": "earliest",
            }
        )
        consumer.subscribe([topic])
        found: list[Any] = []
        deadline = time.monotonic() + timeout
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            message = consumer.poll(0.5)
            if message is not None and message.error() is None:
                found.append(message)
                quiet_since = time.monotonic()
            elif len(found) >= count and time.monotonic() - quiet_since > 2:
                break
        consumer.close()
        assert len(found) >= count, f"{len(found)} of {count} messages on {topic}"
        return found

    def scores(self, count: int) -> dict[str, set[str]]:
        """Every distinct `score.produced` payload, by transaction ID."""
        found: dict[str, set[str]] = defaultdict(set)
        for message in self.read(self.topics.scores, count):
            event = decode(ScoreProduced, message.value())
            assert message.key().decode() == event.transaction_id
            found[event.transaction_id].add(message.value().decode())
        return dict(found)

    def count(self, table: str) -> int:
        with create_engine(self.database).connect() as connection:
            return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def run_until(worker: ScoringWorker, events: int, *, timeout: float = 60) -> None:
    """Run batches until the worker has handled `events` Transactions."""
    handled = 0
    deadline = time.monotonic() + timeout
    while handled < events:
        assert time.monotonic() < deadline, f"handled {handled} of {events}"
        handled += worker.run_batch()


def idle(worker: ScoringWorker, *, polls: int = 6) -> bool:
    """True if nothing is delivered to the worker over a few polls."""
    return all(worker.run_batch() == 0 for _ in range(polls))


@pytest.fixture
def stream(redpanda: str, database: str) -> Iterator[Stream]:
    """A hand-built world: three Wallets and an Agent, and the Subject wallet's routine."""
    topics = Topics(f"test{next(_prefixes)}.")
    ensure_topics(redpanda, topics, partitions=3)
    engine = create_engine(database)
    with engine.begin() as connection:
        for wallet in (SUBJECT, FRIEND, STRANGER):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )
        records.add_agent(connection, AGENT, region="Ashanti")
        for t in map(on_app, routine()):
            records.record_transaction(connection, t)
    engine.dispose()
    yield Stream(redpanda, topics, database)


def test_a_sim_swap_event_then_a_drain_is_scored_high_and_published(stream: Stream):
    usual = on_app(tx(NOW - HOUR))
    swap = SimSwapRecorded(wallet_id=SUBJECT, occurred_at=NOW - 2 * HOUR)
    stream.publish(swap, TransactionCreated.of(usual), TransactionCreated.of(drain()))

    worker = stream.worker()
    run_until(worker, 2)
    worker.close()

    scores = {i: decode(ScoreProduced, next(iter(p)).encode()) for i, p in stream.scores(2).items()}
    by_band = {s.risk_band: s for s in scores.values()}
    assert set(by_band) == {"Low", "High"}
    high = by_band["High"]
    assert high.reasons[0].code == "SIM_SWAP_DRAIN"
    assert high.subject_wallet_id == SUBJECT
    assert high.assessed_at == NOW
    assert stream.count("sim_swaps") == 1
    assert stream.count("risk_assessments") == 2
    assert stream.count("alerts") == 1


class CrashAfter:
    """Scores the first `stored` Transactions, then fails as if the worker died mid-batch."""

    def __init__(self, scoring: StoredScoring, stored: int) -> None:
        self._scoring = scoring
        self._left = stored

    def assess(self, transaction: Transaction) -> Scored:
        if self._left == 0:
            raise RuntimeError("worker died")
        self._left -= 1
        return self._scoring.assess(transaction)

    def record_sim_swap(self, wallet_id: str, occurred_at: datetime) -> bool:
        return self._scoring.record_sim_swap(wallet_id, occurred_at)


def _day_of_transactions() -> list[Transaction]:
    """Six Transactions after the routine, one of them a drain soon after a SIM swap."""
    return [
        on_app(tx(NOW + i * HOUR, amount=40.0 + i)) if i != 3 else drain(NOW + i * HOUR)
        for i in range(6)
    ]


def test_a_worker_that_dies_mid_batch_leaves_it_to_be_finished_exactly_once(stream: Stream):
    day = _day_of_transactions()
    stream.publish(
        SimSwapRecorded(wallet_id=SUBJECT, occurred_at=NOW + 2 * HOUR),
        *map(TransactionCreated.of, day),
    )

    dying = stream.worker(CrashAfter(stream.scoring(), stored=4))
    with pytest.raises(RuntimeError, match="worker died"):
        run_until(dying, len(day))
    dying.close()
    assert stream.count("risk_assessments") == 4

    resumed = stream.worker()
    run_until(resumed, len(day))
    assert idle(resumed)
    resumed.close()

    assert stream.count("risk_assessments") == len(day)
    assert stream.count("alerts") == 1
    # Whatever the dead worker had already sent, each Transaction has one result.
    scores = stream.scores(len(day))
    assert set(scores) == {t.transaction_id for t in day}
    assert all(len(payloads) == 1 for payloads in scores.values())


def test_redelivered_events_republish_the_stored_results_without_scoring_again(stream: Stream):
    day = _day_of_transactions()
    stream.publish(
        SimSwapRecorded(wallet_id=SUBJECT, occurred_at=NOW + 2 * HOUR),
        *map(TransactionCreated.of, day),
    )
    first = stream.worker()
    run_until(first, len(day))
    first.close()
    before = stream.scores(len(day))

    # A new consumer group has every event delivered again from the start.
    again = stream.worker(group="replayed")
    run_until(again, len(day))
    again.close()

    after = stream.scores(2 * len(day))
    assert len(stream.read(stream.topics.scores, 2 * len(day))) == 2 * len(day)
    assert after == before
    assert all(len(payloads) == 1 for payloads in after.values())
    assert stream.count("risk_assessments") == len(day)
    assert stream.count("alerts") == 1


def test_a_restarted_worker_resumes_after_what_it_committed(stream: Stream):
    day = _day_of_transactions()
    stream.publish(*map(TransactionCreated.of, day[:3]))
    first = stream.worker()
    run_until(first, 3)
    first.close()

    stream.publish(*map(TransactionCreated.of, day[3:]))
    restarted = stream.worker()
    run_until(restarted, 3)
    assert idle(restarted)
    restarted.close()

    assert set(stream.scores(len(day))) == {t.transaction_id for t in day}
    assert stream.count("risk_assessments") == len(day)


def test_events_that_break_the_contract_go_to_the_dead_letter_topics(stream: Stream):
    valid = on_app(tx(NOW))
    from_unknown = TransactionCreated.of(on_app(tx(NOW, wallet="w-unknown")))
    stream.publish_raw(stream.topics.transactions, b"not json")
    stream.publish_raw(
        stream.topics.transactions, json.dumps({"transaction_id": "T-only"}).encode()
    )
    stream.publish(from_unknown, SimSwapRecorded(wallet_id="w-unknown", occurred_at=NOW - DAY))
    stream.publish_raw(stream.topics.sim_swaps, b"{}")
    stream.publish(TransactionCreated.of(valid))

    worker = stream.worker()
    run_until(worker, 4)
    assert idle(worker)
    worker.close()

    rejected = {
        m.value(): dict(m.headers())["error"].decode()
        for m in stream.read(Topics.dead_letter(stream.topics.transactions), 3)
    }
    assert len(rejected) == 3
    assert rejected[b"not json"].startswith("InvalidEvent:")
    assert rejected[b'{"transaction_id": "T-only"}'].startswith("InvalidEvent:")
    assert rejected[encode(from_unknown)].startswith("UnknownParty: subject_wallet_id=w-unknown")
    swaps = stream.read(Topics.dead_letter(stream.topics.sim_swaps), 2)
    assert len(swaps) == 2
    assert sorted(dict(m.headers())["error"].decode().split(":")[0] for m in swaps) == [
        "InvalidEvent",
        "UnknownParty",
    ]
    assert set(stream.scores(1)) == {valid.transaction_id}
    assert stream.count("risk_assessments") == 1


def test_ensuring_topics_twice_keeps_what_is_there(stream: Stream):
    ensure_topics(stream.bootstrap, stream.topics, partitions=5)

    consumer = Consumer({"bootstrap.servers": stream.bootstrap, "group.id": "metadata"})
    metadata = consumer.list_topics(timeout=10)
    consumer.close()
    partitions = {
        name: len(topic.partitions)
        for name, topic in metadata.topics.items()
        if name.startswith(stream.topics.prefix)
    }
    assert partitions == {
        stream.topics.transactions: 3,
        stream.topics.scores: 3,
        stream.topics.sim_swaps: 1,
        stream.topics.fraud_discoveries: 1,
        Topics.dead_letter(stream.topics.transactions): 1,
        Topics.dead_letter(stream.topics.sim_swaps): 1,
    }
