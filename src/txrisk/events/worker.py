"""The scoring worker: consumes `transaction.created`, scores, and publishes `score.produced`.

Each Transaction is scored exactly once by its transaction ID, however often it is delivered:

- Scoring goes through `StoredScoring`, which is idempotent on transaction ID. The first delivery
  stores the Risk assessment (and any Alert); every later one gets the stored assessment back.
- `score.produced` always carries the stored assessment, so a redelivered event republishes the
  same result, never a different one. Consumers of `score.produced` can treat a repeat for a
  transaction ID as a duplicate.
- A batch's offsets are committed only after its assessments are stored and its results are
  confirmed by the broker. A worker that stops anywhere before that has its batch redelivered,
  to itself or another worker, which finishes the job without scoring anything twice.

SIM swaps arrive on `sim_swap.recorded`. The features need every SIM swap before a Transaction
to be in the history when it is scored, so before scoring a batch the worker reads SIM swaps up
to the end of their topic. The ledger confirms each SIM swap before publishing any later
Transaction, so once a Transaction has been consumed, every earlier SIM swap is on the topic.
Every worker reads every SIM swap partition; storing one is idempotent.

Transactions are keyed by Subject wallet, so each Subject wallet's own history is scored in
order. Histories across Subject wallets (Transfers into a Wallet, an Agent's Cash-outs) span
partitions, and are as complete as the workers on the other partitions have got.

When the Champion model is down or slow, Transactions are scored in Degraded mode (Rule hits
alone, marked degraded) and published like any other. After the model recovers, the worker
re-scores what was left degraded, between batches; the stored assessments are replaced, and the
`score.produced` already published for them stays as it was (consumers see `degraded` on it).

An event that breaks its contract, or names a party that is not registered, cannot succeed on
any retry: it goes to its topic's dead-letter topic with the reason, and is committed past.
Anything else that fails (the database or the broker) stops the worker without committing, and
its batch is redelivered when it, or another worker, resumes.
"""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from confluent_kafka import Consumer, KafkaError, KafkaException, Message, TopicPartition

from txrisk.events.contracts import (
    InvalidEvent,
    ScoreProduced,
    SimSwapRecorded,
    TransactionCreated,
    decode,
)
from txrisk.events.stream import Publisher, Topics
from txrisk.features import Transaction
from txrisk.store import Rescored, Scored, TransactionConflict, UnknownParty

log = logging.getLogger(__name__)

# Commit failures that only mean the group rebalanced under the batch: its events are
# redelivered to whichever worker now owns their partitions, which replays the stored results.
_REBALANCED = {
    KafkaError.REBALANCE_IN_PROGRESS,
    KafkaError.ILLEGAL_GENERATION,
    KafkaError.UNKNOWN_MEMBER_ID,
}


class Scoring(Protocol):
    """What the worker scores with and writes history to: `store.StoredScoring`."""

    def assess(self, transaction: Transaction) -> Scored: ...

    def record_sim_swap(self, wallet_id: str, occurred_at: datetime) -> bool: ...

    def rescore_due(self) -> bool: ...

    def rescore_degraded(self, limit: int | None = None) -> Rescored: ...


class ScoringWorker:
    def __init__(
        self,
        scoring: Scoring,
        publisher: Publisher,
        *,
        bootstrap_servers: str,
        topics: Topics | None = None,
        group_id: str = "txrisk-scoring",
        batch_size: int = 500,
        poll_timeout: float = 1.0,
        catch_up_timeout: float = 60.0,
        rescore_batch: int = 500,
    ) -> None:
        self._scoring = scoring
        self._publisher = publisher
        self._topics = topics = topics or Topics()
        self._batch_size = batch_size
        self._poll_timeout = poll_timeout
        self._catch_up_timeout = catch_up_timeout
        self._rescore_batch = rescore_batch
        self._transactions = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        self._transactions.subscribe([topics.transactions])
        # Not a group member: every worker is assigned every SIM swap partition. The group only
        # remembers how far they have been read, so a restart does not read them all again.
        self._sim_swaps = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": f"{group_id}.sim-swaps",
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        metadata = self._sim_swaps.list_topics(topics.sim_swaps, timeout=30)
        found = metadata.topics[topics.sim_swaps]
        if found.error is not None:
            raise KafkaException(found.error)
        partitions = [TopicPartition(topics.sim_swaps, p) for p in sorted(found.partitions)]
        self._sim_swaps.assign(partitions)
        self._next_sim_swap = {
            tp.partition: self._start(tp)
            for tp in self._sim_swaps.committed(partitions, timeout=30)
        }

    def run(self, should_stop: Callable[[], bool]) -> None:
        """Score batches until `should_stop()`; a batch under way is always finished first."""
        try:
            while not should_stop():
                self.run_batch()
        finally:
            self.close()

    def run_batch(self) -> int:
        """Consume, score and publish one batch of Transactions, then commit it. Returns how
        many events it held; 0 if none arrived within the poll timeout."""
        messages = self._transactions.consume(self._batch_size, self._poll_timeout)
        if not messages:
            self._rescore_after_recovery()
            return 0
        self.catch_up_sim_swaps()
        offsets: dict[tuple[str, int], int] = {}
        for message in messages:
            if _failed(message):
                continue
            self._handle_transaction(message)
            offsets[(_topic(message), _partition(message))] = _offset(message) + 1
        self._publisher.flush()
        self._commit(self._transactions, offsets)
        self._rescore_after_recovery()
        return len(messages)

    def catch_up_sim_swaps(self) -> int:
        """Store every SIM swap on the topic now, and commit; returns how many were read."""
        ends = {
            partition: self._sim_swaps.get_watermark_offsets(
                TopicPartition(self._topics.sim_swaps, partition), timeout=30, cached=False
            )[1]
            for partition in self._next_sim_swap
        }
        deadline = time.monotonic() + self._catch_up_timeout
        read = 0
        while any(self._next_sim_swap[p] < end for p, end in ends.items()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not read {self._topics.sim_swaps} up to {ends}")
            message = self._sim_swaps.poll(self._poll_timeout)
            if message is None:
                self._skip_gaps()
                continue
            if _failed(message):
                continue
            self._handle_sim_swap(message)
            self._next_sim_swap[_partition(message)] = _offset(message) + 1
            read += 1
        if read:
            self._publisher.flush()
            self._commit(
                self._sim_swaps,
                {(self._topics.sim_swaps, p): n for p, n in self._next_sim_swap.items()},
            )
        return read

    def close(self) -> None:
        self._transactions.close()
        self._sim_swaps.close()

    def assess(self, transaction: Transaction) -> Scored:
        """The one scoring call: everything about how a Transaction is scored sits behind it."""
        return self._scoring.assess(transaction)

    def _rescore_after_recovery(self) -> None:
        """Re-score the Transactions Degraded mode left behind, once the model has recovered.
        A failure here is logged, not fatal: scoring must go on, and `txrisk rescore` or the
        next recovery picks the rest up."""
        if not self._scoring.rescore_due():
            return
        try:
            result = self._scoring.rescore_degraded(limit=self._rescore_batch)
        except Exception:
            log.exception("re-scoring after recovery failed")
            return
        log.info(
            "re-scored %d degraded Transactions (%d Alerts raised), %d remain",
            result.rescored,
            result.alerts_raised,
            result.remaining,
        )

    def _handle_transaction(self, message: Message) -> None:
        try:
            transaction = decode(TransactionCreated, message.value() or b"").to_transaction()
            scored = self.assess(transaction)
        except (InvalidEvent, UnknownParty, TransactionConflict) as e:
            self._reject(message, e)
            return
        self._publisher.publish(
            self._topics.scores,
            transaction.transaction_id,
            ScoreProduced.of(transaction, scored.assessment),
        )

    def _handle_sim_swap(self, message: Message) -> None:
        try:
            event = decode(SimSwapRecorded, message.value() or b"")
            self._scoring.record_sim_swap(event.wallet_id, event.occurred_at)
        except (InvalidEvent, UnknownParty) as e:
            self._reject(message, e)

    def _reject(self, message: Message, error: Exception) -> None:
        topic = _topic(message)
        reason = f"{type(error).__name__}: {error}"
        log.warning("rejected %s[%d]@%d: %s", topic, _partition(message), _offset(message), reason)
        self._publisher.publish_raw(
            Topics.dead_letter(topic),
            message.key(),
            message.value(),
            headers={
                "error": reason[:4096],
                "topic": topic,
                "partition": str(_partition(message)),
                "offset": str(_offset(message)),
            },
        )

    def _start(self, committed: TopicPartition) -> int:
        """Where reading a SIM swap partition starts: its committed offset, else its beginning."""
        if committed.offset >= 0:
            return committed.offset
        low, _ = self._sim_swaps.get_watermark_offsets(committed, timeout=30, cached=False)
        return int(low)

    def _skip_gaps(self) -> None:
        """Move past offsets that hold no event (a compacted or transactional topic has them)."""
        assigned = [TopicPartition(self._topics.sim_swaps, p) for p in self._next_sim_swap]
        for tp in self._sim_swaps.position(assigned):
            if tp.offset >= 0:
                self._next_sim_swap[tp.partition] = max(
                    self._next_sim_swap[tp.partition], tp.offset
                )

    @staticmethod
    def _commit(consumer: Consumer, offsets: dict[tuple[str, int], int]) -> None:
        if not offsets:
            return
        try:
            consumer.commit(
                offsets=[TopicPartition(t, p, o) for (t, p), o in offsets.items()],
                asynchronous=False,
            )
        except KafkaException as e:
            if e.args[0].code() not in _REBALANCED:
                raise
            log.warning("offsets not committed, the group rebalanced: %s", e)


def _failed(message: Message) -> bool:
    """True for a consumer error in place of an event. A fatal one stops the worker; the client
    recovers from any other by itself, so it is only logged."""
    error = message.error()
    if error is None:
        return False
    if error.fatal():
        raise KafkaException(error)
    log.warning("consumer error: %s", error.str())
    return True


def _topic(message: Message) -> str:
    topic = message.topic()
    assert topic is not None
    return topic


def _partition(message: Message) -> int:
    partition = message.partition()
    assert partition is not None
    return partition


def _offset(message: Message) -> int:
    offset = message.offset()
    assert offset is not None
    return offset
