"""Event ingestion: the event contracts, the ledger producer and the scoring workers (ADR 0004)."""

from txrisk.events.contracts import (
    EVENTS,
    FraudDiscovered,
    InvalidEvent,
    ScoreProduced,
    SimSwapRecorded,
    TransactionCreated,
    decode,
    encode,
    json_schema,
    schema_file,
)
from txrisk.events.ledger import Published, ledger_events, publish_ledger
from txrisk.events.settings import WorkerSettings
from txrisk.events.stream import DeliveryFailed, Publisher, Topics, ensure_topics
from txrisk.events.worker import ScoringWorker

__all__ = [
    "EVENTS",
    "DeliveryFailed",
    "FraudDiscovered",
    "InvalidEvent",
    "Published",
    "Publisher",
    "ScoreProduced",
    "ScoringWorker",
    "SimSwapRecorded",
    "Topics",
    "TransactionCreated",
    "WorkerSettings",
    "decode",
    "encode",
    "ensure_topics",
    "json_schema",
    "ledger_events",
    "publish_ledger",
    "schema_file",
]
