"""Measuring the stream path under load: how fast the workers consume, and how the lag recovers.

A stream run publishes Transactions at a set rate for a while, and meanwhile `ConsumerLag` reads,
once a second, how far the workers' group has got (its committed offsets) and how far the topic
goes (its end offsets). `summarise_stream` turns those samples into the numbers the targets are
about. Offsets are counted from the run's start, so earlier runs on the same topic do not count.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from confluent_kafka import Consumer, TopicPartition

# A partition the group has not committed to yet has this offset in the Kafka protocol.
_NO_OFFSET = -1001


@dataclass(frozen=True)
class LagSample:
    """The workers' progress `at` seconds into a run: the offsets, summed over every partition."""

    at: float
    committed: int
    end: int

    @property
    def lag(self) -> int:
        return self.end - self.committed


@dataclass(frozen=True)
class StreamResult:
    """What a stream run showed, for `check_stream`."""

    # Transactions published, and the Transactions a second they were offered at.
    published: int
    offered_per_second: float
    # Transactions a second the workers committed over the time they were being offered.
    consumed_per_second: float
    peak_lag: int
    # Seconds from the end of publishing until the lag first fell to the recovered level, or
    # None if it never did within the samples.
    recovery_seconds: float | None
    # The lag counted as recovered, in events.
    recovered_below: int


def summarise_stream(
    samples: Sequence[LagSample], *, offered_until: float, recovered_below: int
) -> StreamResult:
    """The result of a run that published until `offered_until` seconds in.

    The first sample is the baseline (taken before anything was published); the throughput is
    what the group committed between it and the last sample at or before `offered_until`.
    """
    if len(samples) < 2:
        raise ValueError("a stream run needs at least two lag samples")
    if offered_until <= 0:
        raise ValueError("offered_until must be after the run's start")
    first = samples[0]
    during = [s for s in samples if s.at <= offered_until]
    last_offered = during[-1]
    window = last_offered.at - first.at
    published = max(s.end for s in samples) - first.end
    offered_at = max(s.end for s in during) - first.end
    recovery = next(
        (
            s.at - offered_until
            for s in samples
            if s.at > offered_until and s.lag <= recovered_below
        ),
        None,
    )
    return StreamResult(
        published=published,
        offered_per_second=offered_at / window if window > 0 else 0.0,
        consumed_per_second=(last_offered.committed - first.committed) / window
        if window > 0
        else 0.0,
        peak_lag=max(s.lag for s in samples),
        recovery_seconds=None if recovery is None else max(recovery, 0.0),
        recovered_below=recovered_below,
    )


class ConsumerLag:
    """Reads a consumer group's committed offsets and a topic's end offsets from the broker.

    It never joins the group, so it takes no partitions from the workers.
    """

    def __init__(self, bootstrap_servers: str, topic: str, group_id: str) -> None:
        self._topic = topic
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap_servers,
                "group.id": group_id,
                "enable.auto.commit": False,
            }
        )

    def read(self, *, at: float) -> LagSample:
        metadata = self._consumer.list_topics(self._topic, timeout=10)
        partitions = [
            TopicPartition(self._topic, p) for p in metadata.topics[self._topic].partitions
        ]
        committed = {
            p.partition: p.offset for p in self._consumer.committed(partitions, timeout=10)
        }
        total_committed = total_end = 0
        for partition in partitions:
            low, high = self._consumer.get_watermark_offsets(partition, timeout=10)
            offset = committed.get(partition.partition, _NO_OFFSET)
            # A group with no commit yet starts at the beginning of the partition.
            total_committed += low if offset == _NO_OFFSET else offset
            total_end += high
        return LagSample(at=at, committed=total_committed, end=total_end)

    def close(self) -> None:
        self._consumer.close()
