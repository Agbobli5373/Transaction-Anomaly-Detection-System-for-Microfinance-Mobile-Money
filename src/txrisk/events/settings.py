"""What a scoring worker runs with, read from `TXRISK_*` environment variables."""

from pydantic import Field

from txrisk.api.settings import ApiSettings
from txrisk.events.stream import Topics


class WorkerSettings(ApiSettings):
    """The API's settings, as the worker scores the same way, plus where the stream is."""

    # host:port[,host:port...] of the Redpanda (Kafka API) brokers.
    kafka_bootstrap_servers: str
    # Prefixed to every topic name, to keep environments sharing a cluster apart.
    topic_prefix: str = ""
    # Partitions for new `transaction.created` and `score.produced` topics: at most this many
    # workers share the work.
    topic_partitions: int = Field(default=3, ge=1)
    # Workers in one consumer group share the Transactions between them.
    consumer_group: str = "txrisk-scoring"
    batch_size: int = Field(default=500, ge=1)

    # The worker serves its Prometheus metrics here (see ApiSettings.metrics_port).
    metrics_port: int | None = Field(default=9100, ge=1, le=65535)

    def topics(self) -> Topics:
        return Topics(self.topic_prefix)
