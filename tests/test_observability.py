"""What the API reports about itself: Prometheus metrics, JSON logs and traces, and that tracing
costs nothing until it is switched on. The worker's side is in `test_observability_worker.py`."""

import dataclasses
import logging
import re
import secrets
import socket
from collections.abc import Iterator
from typing import Any

import numpy as np
import pandas as pd
import pytest
import redis
from api_helpers import body, issue_key, settings, with_key
from fastapi.testclient import TestClient
from observability_helpers import capture_logs, capture_spans, sample, scrape
from scoring_helpers import HOUR, NOW, REGISTERED, SUBJECT, Market, routine, tx

from txrisk.api import create_app
from txrisk.features import Transaction
from txrisk.models import INPUT_COLUMNS
from txrisk.models.anomaly import AnomalyModel
from txrisk.observability import logs, tracing
from txrisk.observability.logs import JsonFormatter
from txrisk.store import create_engine, records

FRIEND = "w-friend"
STRANGER = "w-stranger"
AGENT = "A00001"
# Built at run time: nothing secret-shaped is written in the source.
MSISDN = "233" + "".join(str(secrets.randbelow(10)) for _ in range(9))


def on_app(transaction: Transaction) -> Transaction:
    return dataclasses.replace(transaction, channel="app")


def drain(at: Any = NOW) -> Transaction:
    """A Transfer of 90% of the balance to a stranger."""
    return on_app(tx(at, amount=900.0, counterparty=STRANGER, balance=1000.0))


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def seed_world(database: str) -> None:
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
        records.add_sim_swap(connection, SUBJECT, NOW - 2 * HOUR)
    engine.dispose()


class Served:
    """The API running over a seeded database, with its metrics port."""

    def __init__(self, client: TestClient, metrics_port: int, key: str) -> None:
        self.client = client
        self.metrics_port = metrics_port
        self.key = key

    def score(self, transaction: Transaction, **headers: str) -> Any:
        return self.client.post("/v1/score", json=body(transaction), headers=headers)

    def metrics(self) -> str:
        return scrape(self.metrics_port)


@pytest.fixture
def served(database: str, redis_url: str) -> Iterator[Served]:
    redis.Redis.from_url(redis_url).flushall()
    seed_world(database)
    port = free_port()
    key = issue_key(database)
    app = create_app(settings(database, metrics_port=port, redis_url=redis_url))
    with TestClient(app, headers=with_key(key)) as client:
        yield Served(client, port, key)


def test_scoring_moves_the_metrics_and_the_scrape_holds_nothing_sensitive(served: Served):
    before = served.metrics()
    transaction = drain()

    first = served.score(transaction)
    again = served.score(transaction)
    stranger = served.score(on_app(tx(NOW, wallet="w-nobody")))

    assert first.status_code == again.status_code == 200
    assert stranger.status_code == 422
    after = served.metrics()
    day = NOW.date().isoformat()
    # One High Risk score and one Alert, however often it is asked for; the repeat is a replay.
    band = {"path": "api", "band": "High", "degraded": "false"}
    assert sample(after, "txrisk_scores_total", **band) == 1
    assert sample(before, "txrisk_scores_total", **band) == 0
    assert sample(after, "txrisk_alerts_total", day=day) == 1
    assert sample(after, "txrisk_score_replays_total", path="api") == 1
    assert sample(after, "txrisk_scoring_errors_total", path="api", reason="unknown_party") == 1
    # Latency is a histogram, and the HTTP metrics are by route template, not URL.
    assert sample(after, "txrisk_scoring_duration_seconds_count", path="api") == 2
    route = {"method": "POST", "route": "/v1/score"}
    assert sample(after, "txrisk_http_requests_total", **route, status="200") == 2
    assert sample(after, "txrisk_http_requests_total", **route, status="422") == 1
    assert sample(after, "txrisk_http_request_duration_seconds_count", **route) == 3
    # Nothing about a Transaction, a Wallet or the caller is in a label or a value.
    for secret in (transaction.transaction_id, SUBJECT, STRANGER, served.key):
        assert secret not in after


def test_metrics_are_not_part_of_the_open_api(served: Served):
    assert served.client.get("/metrics").status_code == 404
    assert served.client.get("/v1/metrics").status_code == 404
    assert "metrics" not in served.client.get("/v1/openapi.json").text


def test_every_log_line_of_a_request_has_the_correlation_id_and_scoring_adds_the_transaction(
    served: Served,
):
    transaction = drain()
    with capture_logs() as captured:
        response = served.score(transaction, **{"X-Correlation-ID": "client-req-7"})
        generated = served.client.get("/v1/models/active")
        hostile = served.client.get("/v1/models/active", headers={"X-Correlation-ID": 'a b"c'})

    assert response.headers["X-Correlation-ID"] == "client-req-7"
    mine = [line for line in captured.lines() if line["correlation_id"] == "client-req-7"]
    # The scoring line and the request line both know the Transaction.
    assert {line["logger"] for line in mine} >= {"txrisk.api.app", "txrisk.api"}
    assert {line["transaction_id"] for line in mine} == {transaction.transaction_id}
    ours = [line for line in captured.lines() if line["logger"].startswith("txrisk")]
    assert ours and all(line["correlation_id"] for line in ours)
    # No correlation id from the caller: one is made, and returned.
    assert re.fullmatch(r"[0-9a-f]{32}", generated.headers["X-Correlation-ID"])
    # One that is not a plain token is replaced rather than copied into the logs.
    assert hostile.headers["X-Correlation-ID"] != 'a b"c'
    assert served.key not in captured.raw()


def test_one_transaction_is_one_trace_through_cache_database_and_model_steps(served: Served):
    first = drain()
    second = on_app(tx(NOW + HOUR, amount=30.0))
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    with capture_spans() as exporter:
        served.score(first, traceparent=f"00-{trace_id}-b7ad6b7169203331-01")
        served.score(second)
    spans = exporter.get_finished_spans()

    # The caller's trace carries on through the API's own spans.
    traced = [s for s in spans if format(s.context.trace_id, "032x") == trace_id]
    assert {s.name for s in traced} >= {
        "POST /v1/score",
        "score.transaction",
        "store.lookup",
        "features.compute",
        "history.redis",
        "history.postgres.subject_history",
        "store.save",
        "cache.record",
    }
    assert len({s.context.trace_id for s in traced}) == 1
    root = next(s for s in traced if s.name == "POST /v1/score")
    assert root.parent is not None and format(root.parent.span_id, "016x") == "b7ad6b7169203331"
    by_id = {s.context.span_id: s for s in traced}

    def ancestors(span: Any) -> list[str]:
        names = []
        while span.parent is not None and span.parent.span_id in by_id:
            span = by_id[span.parent.span_id]
            names.append(span.name)
        return names

    features = next(s for s in traced if s.name == "features.compute")
    assert ancestors(features) == ["score.transaction", "POST /v1/score"]
    # The first read finds nothing cached, so Postgres answers; once the first Transaction is
    # stored and cached, the second one's history is served from Redis.
    reads = [s for s in spans if s.name == "history.redis"]
    hits = {s.attributes["cache.hit"] for s in reads if s.attributes is not None}
    assert hits == {True, False}
    # A request with no `traceparent` is a trace of its own, with the same spans.
    others = {s.context.trace_id for s in spans} - {traced[0].context.trace_id}
    assert len(others) == 1


def test_a_model_scores_inside_a_span_of_its_own():
    rng = np.random.default_rng(0)
    training = pd.DataFrame(rng.normal(1, 0.1, (500, len(INPUT_COLUMNS))), columns=INPUT_COLUMNS)
    model = AnomalyModel.fit(training, seed=1)
    market = Market().record(*routine())

    with capture_spans() as exporter:
        market.assess(tx(transaction_type="payment", amount=300.0), anomaly=model)

    names = [s.name for s in exporter.get_finished_spans()]
    assert "model.score" in names and "features.compute" in names


def test_tracing_is_off_unless_the_environment_enables_it():
    assert tracing.configure_tracing("txrisk-test", {}) is None
    assert tracing.configure_tracing("txrisk-test", {"TXRISK_TRACING_ENABLED": "false"}) is None

    # No provider: spans are the API's no-op ones, and nothing is put in event headers.
    with tracing.span("anything") as span:
        assert not span.is_recording()
        assert tracing.inject() == {}
    assert tracing.extract([("other", b"x")]) is None


def test_enabling_tracing_installs_an_exporting_provider_and_disabling_it_restores_nothing():
    environ = {
        "TXRISK_TRACING_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:1",
    }
    provider = tracing.configure_tracing("txrisk-test", environ)
    try:
        assert provider is not None
        with tracing.span("on") as span:
            assert span.is_recording()
            assert "traceparent" in tracing.inject()
    finally:
        tracing.use_provider(None)
        if provider is not None:
            provider.shutdown()
    with tracing.span("off") as span:
        assert not span.is_recording()


def test_log_lines_are_json_with_the_ids_and_never_secrets_or_msisdns():
    credential = secrets.token_hex(16)
    header = "Authorization: " + " ".join(["Bearer", credential])
    key_assignment = "x-api-key=" + secrets.token_hex(16)
    spaced = " ".join([MSISDN[:3], MSISDN[3:5], MSISDN[5:8], MSISDN[8:]])
    with capture_logs() as captured:
        log = logging.getLogger("txrisk.test")
        log.info("outside any request")
        with logs.scope("corr-1") as context:
            logs.bind_transaction("T123")
            log.info("scoring %s", "T123", extra={"risk_band": "High"})
            log.warning("sent %s with %s and %s for +%s", spaced, header, key_assignment, MSISDN)
            try:
                raise ValueError(f"bad wallet {MSISDN}")
            except ValueError:
                log.exception("failed")
            assert context.transaction_id == "T123"

    outside, scoring, warned, failed = captured.lines()
    assert outside["correlation_id"] is None and outside["transaction_id"] is None
    assert scoring["correlation_id"] == "corr-1" and scoring["transaction_id"] == "T123"
    assert scoring["message"] == "scoring T123" and scoring["risk_band"] == "High"
    assert scoring["level"] == "INFO" and scoring["logger"] == "txrisk.test"
    assert {"correlation_id", "transaction_id"} <= set(warned) and warned["level"] == "WARNING"
    for leaky in (credential, key_assignment.split("=")[1], MSISDN, spaced):
        assert leaky not in captured.raw()
    assert "ValueError" in failed["exception"]
    assert JsonFormatter().format(logging.LogRecord("x", 20, "", 0, "plain", None, None))


def test_log_lines_carry_the_trace_when_tracing():
    with capture_spans(), capture_logs() as captured, tracing.span("work") as span:
        logging.getLogger("txrisk.test").info("inside")

    line = captured.lines()[0]
    assert line["trace_id"] == format(span.get_span_context().trace_id, "032x")
    assert line["span_id"] == format(span.get_span_context().span_id, "016x")
