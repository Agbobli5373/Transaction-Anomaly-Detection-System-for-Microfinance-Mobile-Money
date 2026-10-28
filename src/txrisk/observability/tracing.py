"""OpenTelemetry tracing, off unless enabled.

Without `TXRISK_TRACING_ENABLED` no provider exists and `span` hands out the API's no-op tracer:
no exporter, no background thread, and nothing is injected into event headers. With it, spans go
to an OTLP/HTTP endpoint (Jaeger takes OTLP on 4318); `OTEL_EXPORTER_OTLP_ENDPOINT` and the other
standard `OTEL_*` variables configure the exporter itself.

The tracer is held here rather than installed as OpenTelemetry's process-wide provider, so each
service names itself and tests can swap in an in-memory exporter.

Trace context crosses Redpanda in the W3C `traceparent` event header (`inject` and `extract`), so
a Transaction published by the producer and scored by a worker is one trace.
"""

import functools
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

ENABLED_ENV = "TXRISK_TRACING_ENABLED"
TRACEPARENT = "traceparent"

_provider: TracerProvider | None = None
_noop = trace.NoOpTracer()
_propagator = TraceContextTextMapPropagator()


def tracing_enabled(environ: Mapping[str, str] = os.environ) -> bool:
    return environ.get(ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def configure_tracing(
    service_name: str, environ: Mapping[str, str] = os.environ
) -> TracerProvider | None:
    """Start exporting spans to the OTLP endpoint if `TXRISK_TRACING_ENABLED` says so; returns
    the provider, or None when tracing stays off."""
    if not tracing_enabled(environ):
        return None
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    use_provider(provider)
    return provider


def use_provider(provider: TracerProvider | None) -> TracerProvider | None:
    """Trace with this provider (None: stop tracing); returns the one it replaces."""
    global _provider
    previous, _provider = _provider, provider
    return previous


def shutdown_tracing() -> None:
    """Flush and stop the exporter, so a short-lived process loses no spans."""
    if _provider is not None:
        _provider.shutdown()


def exporting_to(service_name: str, exporter: SpanExporter) -> TracerProvider:
    """A provider that hands each span to `exporter` as it ends, for tests."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider


def tracer() -> trace.Tracer:
    return _noop if _provider is None else _provider.get_tracer("txrisk")


@contextmanager
def span(
    name: str,
    *,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
    context: otel_context.Context | None = None,
    **attributes: Any,
) -> Iterator[trace.Span]:
    """A span named `name` under the current one, ended on exit; an exception marks it failed."""
    with tracer().start_as_current_span(
        name, context=context, kind=kind, attributes=attributes or None
    ) as current:
        yield current


_P = ParamSpec("_P")
_R = TypeVar("_R")


def traced(name: str) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Run the decorated function inside a span called `name`."""

    def decorate(function: Callable[_P, _R]) -> Callable[_P, _R]:
        @functools.wraps(function)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(name):
                return function(*args, **kwargs)

        return wrapper

    return decorate


def inject() -> dict[str, str]:
    """The `traceparent` of the current span, for an event header; empty when not tracing."""
    carrier: dict[str, str] = {}
    _propagator.inject(carrier)
    return carrier


def extract(headers: Sequence[tuple[str, Any]] | None) -> otel_context.Context | None:
    """The trace context an event's headers carry, if any."""
    for key, value in headers or ():
        if key == TRACEPARENT and value is not None:
            text = value.decode() if isinstance(value, bytes) else str(value)
            return _propagator.extract({TRACEPARENT: text})
    return None
