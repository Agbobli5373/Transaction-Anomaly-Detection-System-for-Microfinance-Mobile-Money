"""Prometheus metrics, one `Metrics` per service process, each with a registry of its own.

Every service exposes its registry over HTTP (`serve`) on a port that is not published beyond the
compose network; the metrics are counts and timings with labels from small closed sets (routes,
Risk bands, topics, partitions, simulated days) and carry no Transaction, Wallet or person data.

The names a dashboard or an alert relies on:

- `txrisk_http_request_duration_seconds`, `txrisk_http_requests_total`: the API, by route;
- `txrisk_scoring_duration_seconds{path}`: scoring one Transaction, `path` being `api` or `worker`;
- `txrisk_scores_total{path,band,degraded}`: Transactions scored for the first time, by Risk band
  and whether Degraded mode scored them; replays of a stored assessment are
  `txrisk_score_replays_total`;
- `txrisk_alerts_total{day}`: Alerts raised, by the simulated day of the Transaction (ADR 0005);
- `txrisk_scoring_errors_total{path,reason}`: Transactions refused or failed;
- `txrisk_events_consumed_total{topic,outcome}`, `txrisk_consumer_lag{topic,partition}`: the worker;
- `txrisk_events_published_total{topic}`, `txrisk_publish_failures_total`: every publisher;
- `txrisk_scheduler_jobs_fired_total{job}`: the scheduler;
- `txrisk_degraded_assessments_total{reason}`, `txrisk_rescored_total`,
  `txrisk_alerts_rescored_total{day}`, `txrisk_model_load_failures_total`: Degraded mode and
  re-scoring, counted through `increment` by the scoring path (`txrisk.scoring.metrics`).
"""

import time
from collections.abc import Callable
from datetime import datetime
from http.server import HTTPServer
from threading import Thread
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, ProcessCollector
from prometheus_client import start_http_server as _start_http_server

from txrisk.scoring.metrics import (
    ALERTS_RESCORED,
    DEGRADED_ASSESSMENTS,
    MODEL_LOAD_FAILURES,
    RESCORED,
)

if TYPE_CHECKING:  # scoring itself is traced, so importing it here at runtime would cycle
    from txrisk.scoring import RiskAssessment

# Scoring one Transaction takes a few milliseconds from Redis history and tens from Postgres.
_LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def stopwatch() -> Callable[[], float]:
    """Starts timing now; the returned function gives the seconds elapsed. The one place latency
    is read from the real clock: it times how long work took and never decides anything."""
    started = time.perf_counter()
    return lambda: time.perf_counter() - started


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        ProcessCollector(registry=self.registry)
        self.http_requests = Counter(
            "txrisk_http_requests",
            "API requests, by route and response status.",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.http_seconds = Histogram(
            "txrisk_http_request_duration_seconds",
            "API request latency, by route.",
            ["method", "route"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.scoring_seconds = Histogram(
            "txrisk_scoring_duration_seconds",
            "Scoring and storing one Transaction.",
            ["path"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.scores = Counter(
            "txrisk_scores",
            "Transactions scored for the first time, by Risk band and Degraded mode.",
            ["path", "band", "degraded"],
            registry=self.registry,
        )
        self.score_replays = Counter(
            "txrisk_score_replays",
            "Transactions whose stored assessment was returned again.",
            ["path"],
            registry=self.registry,
        )
        self.scoring_errors = Counter(
            "txrisk_scoring_errors",
            "Transactions refused or failed, by reason.",
            ["path", "reason"],
            registry=self.registry,
        )
        self.alerts = Counter(
            "txrisk_alerts",
            "Alerts raised, by the simulated day of the Transaction.",
            ["day"],
            registry=self.registry,
        )
        self.events_consumed = Counter(
            "txrisk_events_consumed",
            "Events the worker consumed, by topic and outcome.",
            ["topic", "outcome"],
            registry=self.registry,
        )
        self.consumer_lag = Gauge(
            "txrisk_consumer_lag",
            "Events on a partition not yet committed by the worker's group.",
            ["topic", "partition"],
            registry=self.registry,
        )
        self.events_published = Counter(
            "txrisk_events_published",
            "Events the broker confirmed, by topic.",
            ["topic"],
            registry=self.registry,
        )
        self.publish_failures = Counter(
            "txrisk_publish_failures",
            "Events the broker did not confirm.",
            registry=self.registry,
        )
        # The scoring path's own counters (see txrisk.scoring.metrics), by name: `increment`.
        self._scoring_counters = {
            DEGRADED_ASSESSMENTS: Counter(
                DEGRADED_ASSESSMENTS.removesuffix("_total"),
                "Transactions scored with Rule hits alone, by why the model was not used.",
                ["reason"],
                registry=self.registry,
            ),
            RESCORED: Counter(
                RESCORED.removesuffix("_total"),
                "Degraded Transactions re-scored by the full model.",
                registry=self.registry,
            ),
            ALERTS_RESCORED: Counter(
                ALERTS_RESCORED.removesuffix("_total"),
                "Alerts raised by re-scoring, by the simulated day of the Transaction.",
                ["day"],
                registry=self.registry,
            ),
            MODEL_LOAD_FAILURES: Counter(
                MODEL_LOAD_FAILURES.removesuffix("_total"),
                "Times loading the Champion model bundle failed.",
                registry=self.registry,
            ),
        }
        self.scheduler_jobs = Counter(
            "txrisk_scheduler_jobs_fired",
            "Scheduled jobs launched, by job.",
            ["job"],
            registry=self.registry,
        )

    def increment(self, name: str, **labels: str) -> None:
        """Add one to a counter of the scoring path (the `txrisk.scoring.Metrics` interface)."""
        counter = self._scoring_counters[name]
        (counter.labels(**labels) if labels else counter).inc()

    def observe_score(
        self,
        path: str,
        assessment: "RiskAssessment",
        *,
        replayed: bool,
        seconds: float,
        occurred_at: datetime,
    ) -> None:
        """Record one scored Transaction: its latency, and unless it was a replay, its Risk band,
        Degraded mode and Alert (only a High Risk band raises one)."""
        self.scoring_seconds.labels(path).observe(seconds)
        if replayed:
            self.score_replays.labels(path).inc()
            return
        self.scores.labels(path, assessment.risk_band.value, str(assessment.degraded).lower()).inc()
        if assessment.risk_band.value == "High":
            self.alerts.labels(occurred_at.date().isoformat()).inc()

    def serve(self, port: int, address: str = "0.0.0.0") -> tuple[HTTPServer, Thread]:
        """Expose this registry at `/metrics` on `port`, in a thread; `port` 0 picks a free one
        (the server's `server_port`). Unauthenticated: publish the port to no one."""
        server, thread = _start_http_server(port, addr=address, registry=self.registry)
        return server, thread
