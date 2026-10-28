"""The Redpanda topics, and publishing to them with every delivery confirmed."""

import logging
from dataclasses import dataclass

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic

from txrisk.events.contracts import Event, encode

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Topics:
    """The topic names, under an optional prefix that keeps environments sharing one cluster
    apart. A rejected event goes to its topic's dead-letter topic, `<topic>.dlq`."""

    prefix: str = ""

    @property
    def transactions(self) -> str:
        return self.prefix + "transaction.created"

    @property
    def sim_swaps(self) -> str:
        return self.prefix + "sim_swap.recorded"

    @property
    def scores(self) -> str:
        return self.prefix + "score.produced"

    @property
    def fraud_discoveries(self) -> str:
        return self.prefix + "fraud.discovered"

    @staticmethod
    def dead_letter(topic: str) -> str:
        return topic + ".dlq"


def ensure_topics(bootstrap_servers: str, topics: Topics, *, partitions: int) -> None:
    """Create any topic that does not exist yet; existing topics are left as they are.

    Transactions and scores get `partitions` partitions, so that many workers can share them.
    SIM swaps get one: every worker reads all of them (see `ScoringWorker`), and they are rare.
    """
    wanted = {
        topics.transactions: partitions,
        topics.scores: partitions,
        topics.sim_swaps: 1,
        topics.fraud_discoveries: 1,
        topics.dead_letter(topics.transactions): 1,
        topics.dead_letter(topics.sim_swaps): 1,
    }
    admin = AdminClient({"bootstrap.servers": bootstrap_servers})
    futures = admin.create_topics(
        [NewTopic(name, num_partitions=n, replication_factor=-1) for name, n in wanted.items()],
        operation_timeout=30,
    )
    for name, future in futures.items():
        try:
            future.result()
            log.info("created topic %s", name)
        except KafkaException as e:
            if e.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise


class DeliveryFailed(Exception):
    """The broker did not confirm a published event."""


class Publisher:
    """Publishes events, idempotently and acknowledged by every in-sync replica.

    `publish` only queues an event; `flush` waits until every queued event is confirmed and
    raises `DeliveryFailed` if any is not. Nothing that depends on an event having been
    published (committing a consumer offset, say) may happen before `flush` returns.
    """

    def __init__(self, bootstrap_servers: str, *, flush_timeout: float = 30.0) -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap_servers,
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 5,
            }
        )
        self._flush_timeout = flush_timeout
        self._failures: list[str] = []

    def publish(self, topic: str, key: str, event: Event) -> None:
        self.publish_raw(topic, key.encode(), encode(event))

    def publish_raw(
        self,
        topic: str,
        key: bytes | None,
        value: bytes | None,
        headers: dict[str, str | bytes | None] | None = None,
    ) -> None:
        while True:
            try:
                self._producer.produce(
                    topic,
                    value=value,
                    key=key,
                    headers=headers,
                    on_delivery=self._delivered,
                )
                break
            except BufferError:
                # The local queue is full: wait for some deliveries, then try again.
                self._producer.poll(1.0)
        self._producer.poll(0)

    def flush(self) -> None:
        remaining = self._producer.flush(self._flush_timeout)
        failures, self._failures = self._failures, []
        if remaining:
            failures.append(f"{remaining} events still unconfirmed after {self._flush_timeout}s")
        if failures:
            raise DeliveryFailed("; ".join(failures))

    def _delivered(self, error: KafkaError | None, message: Message) -> None:
        if error is not None:
            self._failures.append(f"{message.topic()}: {error.str()}")
