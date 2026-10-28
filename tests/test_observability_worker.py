"""What the worker reports about itself, against real Redpanda and Postgres: metrics (consumer lag
among them), JSON logs and one trace from the producer's publish to the score it publishes."""

from typing import Any

from observability_helpers import capture_logs, capture_spans, render, sample, scrape, total
from scoring_helpers import HOUR, NOW, SUBJECT, tx
from test_stream import Stream, drain, on_app, run_until
from test_stream import stream as stream_fixture

from txrisk.events import Publisher, ScoringWorker, SimSwapRecorded, TransactionCreated
from txrisk.events.stream import CORRELATION_HEADER, header_value
from txrisk.observability import logs, tracing
from txrisk.observability.metrics import Metrics

# test_stream.py's fixture: its own topics on the broker, and a database with a hand-built world.
stream = stream_fixture


def worker_of(stream: Stream, metrics: Metrics, *, batch_size: int = 500) -> ScoringWorker:
    return ScoringWorker(
        stream.scoring(),
        Publisher(stream.bootstrap, metrics=metrics),
        bootstrap_servers=stream.bootstrap,
        topics=stream.topics,
        group_id=f"{stream.topics.prefix}workers",
        batch_size=batch_size,
        poll_timeout=0.5,
        metrics=metrics,
    )


def headers_of(message: Any) -> dict[str, str]:
    return {k: v.decode() for k, v in message.headers() or []}


def test_one_transaction_is_one_trace_from_the_producer_to_the_published_score(stream: Stream):
    transaction = drain()
    with capture_spans() as exporter, capture_logs() as captured:
        publisher = Publisher(stream.bootstrap)
        # A producer that already has a correlation id passes it on, and its trace.
        with logs.scope("order-42"), tracing.span("producer") as root:
            publisher.publish(
                stream.topics.transactions,
                SUBJECT,
                TransactionCreated.of(transaction),
            )
            publisher.flush()
        worker = worker_of(stream, Metrics())
        run_until(worker, 1)
        worker.close()
        spans = exporter.get_finished_spans()
        (score,) = stream.read(stream.topics.scores, 1)

    # One trace, whichever side of Redpanda a span is on.
    trace_id = root.get_span_context().trace_id
    assert {s.context.trace_id for s in spans} == {trace_id}
    names = {s.name for s in spans}
    assert names >= {
        f"publish {stream.topics.transactions}",
        f"consume {stream.topics.transactions}",
        "score.transaction",
        "store.lookup",
        "features.compute",
        "history.postgres.subject_history",
        "store.save",
        f"publish {stream.topics.scores}",
    }
    consumed = next(s for s in spans if s.name == f"consume {stream.topics.transactions}")
    published = next(s for s in spans if s.name == f"publish {stream.topics.transactions}")
    assert consumed.parent is not None
    assert consumed.parent.span_id == published.context.span_id
    # The score carries the same correlation id and the trace on.
    sent = headers_of(score)
    assert sent[CORRELATION_HEADER] == "order-42"
    assert int(sent["traceparent"].split("-")[1], 16) == trace_id
    # Every log line of handling the event has both ids.
    handling = [
        line for line in captured.lines() if line["transaction_id"] == transaction.transaction_id
    ]
    assert handling and {line["correlation_id"] for line in handling} == {"order-42"}
    assert any(line["message"].startswith("scored") for line in handling)


def test_an_event_without_a_correlation_id_gets_one_and_the_score_carries_it(stream: Stream):
    stream.publish(TransactionCreated.of(on_app(tx(NOW))))
    with capture_logs() as captured:
        worker = worker_of(stream, Metrics())
        run_until(worker, 1)
        worker.close()
    (score,) = stream.read(stream.topics.scores, 1)

    generated = header_value(list(headers_of(score).items()), CORRELATION_HEADER)
    assert generated is not None and len(generated) == 32
    scored = [line for line in captured.lines() if line["message"].startswith("scored")]
    assert [line["correlation_id"] for line in scored] == [generated]
    # Tracing is off, so no trace context is put on the event.
    assert "traceparent" not in headers_of(score)


def test_the_worker_exports_throughput_scores_by_band_and_alerts_per_day(stream: Stream):
    transaction = drain()
    stream.publish(
        SimSwapRecorded(wallet_id=SUBJECT, occurred_at=NOW - 2 * HOUR),
        TransactionCreated.of(on_app(tx(NOW - HOUR))),
        TransactionCreated.of(transaction),
    )
    metrics = Metrics()
    server, _ = metrics.serve(0, "127.0.0.1")
    worker = worker_of(stream, metrics)
    try:
        run_until(worker, 2)
        worker.close()
        text = scrape(server.server_port)
    finally:
        server.shutdown()
        server.server_close()

    topic = stream.topics.transactions
    assert sample(text, "txrisk_events_consumed_total", topic=topic, outcome="scored") == 2
    assert sample(text, "txrisk_scores_total", path="worker", band="High", degraded="false") == 1
    assert sample(text, "txrisk_scores_total", path="worker", band="Low", degraded="false") == 1
    assert sample(text, "txrisk_alerts_total", day=NOW.date().isoformat()) == 1
    assert sample(text, "txrisk_scoring_duration_seconds_count", path="worker") == 2
    assert sample(text, "txrisk_events_published_total", topic=stream.topics.scores) == 2
    assert transaction.transaction_id not in text and SUBJECT not in text


def test_a_rejected_event_is_counted_and_dead_lettered_with_its_correlation_id(stream: Stream):
    stream.publish_raw(stream.topics.transactions, b"not an event")
    metrics = Metrics()
    with capture_logs() as captured:
        worker = worker_of(stream, metrics)
        run_until(worker, 1)
        worker.close()
    (dead,) = stream.read(stream.topics.dead_letter(stream.topics.transactions), 1)

    text = render(metrics)
    assert (
        sample(
            text,
            "txrisk_events_consumed_total",
            topic=stream.topics.transactions,
            outcome="rejected",
        )
        == 1
    )
    assert total(text, "txrisk_scoring_errors_total", path="worker") == 1
    rejected = [line for line in captured.lines() if line["message"].startswith("rejected")]
    assert [line["correlation_id"] for line in rejected] == [headers_of(dead)[CORRELATION_HEADER]]


def test_consumer_lag_is_what_the_group_has_not_yet_committed(stream: Stream):
    events = [TransactionCreated.of(on_app(tx(NOW + i * HOUR))) for i in range(3)]
    stream.publish(*events)
    metrics = Metrics()
    worker = worker_of(stream, metrics, batch_size=1)
    try:
        run_until(worker, 1)
        after_one = render(metrics)
        run_until(worker, 2)
        after_all = render(metrics)
    finally:
        worker.close()

    # One partition is assigned a Transaction at a time, so lag is what is still to come.
    assert total(after_one, "txrisk_consumer_lag", topic=stream.topics.transactions) == 2
    assert total(after_all, "txrisk_consumer_lag", topic=stream.topics.transactions) == 0
