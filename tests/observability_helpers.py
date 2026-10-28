"""Reading what a service reports about itself in tests: metrics, JSON log lines and spans."""

import io
import json
import logging
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from txrisk.observability import tracing
from txrisk.observability.logs import JsonFormatter
from txrisk.observability.metrics import Metrics


def scrape(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=10) as response:
        return str(response.read().decode())


def render(metrics: Metrics) -> str:
    """What a scrape of these metrics would return, without serving them."""
    return generate_latest(metrics.registry).decode()


def sample(text: str, name: str, **labels: str) -> float:
    """The value of the series `name` with exactly these labels, 0 if it has not been seen yet.
    Counters are named as exposed, with `_total` (histograms: `_count`, `_bucket`)."""
    for family in text_string_to_metric_families(text):
        for found in family.samples:
            if found.name == name and found.labels == labels:
                return float(found.value)
    return 0.0


def total(text: str, name: str, **labels: str) -> float:
    """The sum of every series `name` whose labels include these."""
    return sum(
        float(found.value)
        for family in text_string_to_metric_families(text)
        for found in family.samples
        if found.name == name and labels.items() <= found.labels.items()
    )


class JsonLogs:
    """Everything logged while it is attached, parsed as the JSON lines a service writes."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def raw(self) -> str:
        return self.stream.getvalue()


@contextmanager
def capture_logs() -> Iterator[JsonLogs]:
    captured = JsonLogs()
    handler = logging.StreamHandler(captured.stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield captured
    finally:
        root.removeHandler(handler)
        root.setLevel(level)


@contextmanager
def capture_spans() -> Iterator[InMemorySpanExporter]:
    """Trace into memory, as `TXRISK_TRACING_ENABLED` traces to an exporter."""
    exporter = InMemorySpanExporter()
    previous = tracing.use_provider(tracing.exporting_to("test", exporter))
    try:
        yield exporter
    finally:
        tracing.use_provider(previous)
