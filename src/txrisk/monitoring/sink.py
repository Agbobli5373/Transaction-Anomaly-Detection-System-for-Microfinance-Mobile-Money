"""The alert sink: a webhook that records the alerts Alertmanager sends it.

Alertmanager POSTs its webhook payload to `/alerts`. The sink counts each alert (by name, status
and severity, with when it last arrived) and exposes the counts on `/metrics`, which Prometheus
scrapes and Grafana shows next to the model's health; `GET /alerts` lists the most recent ones as
JSON. It stands in for a pager or chat integration and keeps nothing across restarts. It holds no
credential and is reachable on the compose network only.
"""

import json
import logging
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

log = logging.getLogger(__name__)

# The most alerts `GET /alerts` keeps.
RECENT = 200


class AlertSink:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        labels = ["alertname", "status", "severity"]
        self._received = Counter(
            "txrisk_alert_sink_received",
            "Alerts received from Alertmanager",
            labels,
            registry=self.registry,
        )
        self._last = Gauge(
            "txrisk_alert_sink_last_received_timestamp_seconds",
            "When an alert of this name last arrived",
            ["alertname"],
            registry=self.registry,
        )
        self._recent: deque[dict[str, Any]] = deque(maxlen=RECENT)
        self._lock = Lock()
        self._sequence = 0

    def receive(self, payload: dict[str, Any]) -> int:
        """Record the alerts of one Alertmanager webhook payload; how many there were."""
        found = payload.get("alerts", [])
        with self._lock:
            for alert in found:
                labels = alert.get("labels", {})
                name = labels.get("alertname", "unknown")
                self._received.labels(
                    name, alert.get("status", "unknown"), labels.get("severity", "none")
                ).inc()
                self._last.labels(name).set_to_current_time()
                self._sequence += 1
                self._recent.append(
                    {
                        "sequence": self._sequence,
                        "status": alert.get("status"),
                        "labels": labels,
                        "annotations": alert.get("annotations", {}),
                        "starts_at": alert.get("startsAt"),
                    }
                )
                log.info("alert %s %s", name, alert.get("status"), extra={"labels": labels})
        return len(found)

    def recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent)

    def metrics(self) -> bytes:
        return generate_latest(self.registry)


def serve(sink: AlertSink, host: str, port: int) -> ThreadingHTTPServer:
    """A server for `sink`, not yet running: `serve_forever()` runs it."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path != "/alerts":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length))
                count = sink.receive(payload)
            except (ValueError, AttributeError):
                self._send(400, b"not an Alertmanager payload", "text/plain")
                return
            self._send(200, json.dumps({"received": count}).encode(), "application/json")

        def do_GET(self) -> None:
            if self.path == "/metrics":
                self._send(200, sink.metrics(), CONTENT_TYPE_LATEST)
            elif self.path == "/alerts":
                self._send(200, json.dumps(sink.recent()).encode(), "application/json")
            elif self.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, format: str, *args: Any) -> None:
            pass  # the sink logs each alert itself

    return ThreadingHTTPServer((host, port), Handler)
