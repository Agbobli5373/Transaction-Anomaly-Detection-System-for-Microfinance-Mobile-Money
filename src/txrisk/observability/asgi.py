"""The API's observability middleware, outermost so a request refused by any other layer (too
large, rate limited, unauthenticated) is still counted, logged and traced.

Per request it takes the correlation id from `X-Correlation-ID` (or makes one) and echoes it in the
response, scopes the logs to it, continues the caller's trace from `traceparent` if there is one,
times the request and logs one line when it ends. The route label is the route's path template,
never the URL, so Transaction IDs and keys never become metric labels.
"""

import logging

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from txrisk.observability import logs
from txrisk.observability.metrics import Metrics, stopwatch
from txrisk.observability.tracing import TRACEPARENT, span

CORRELATION_HEADER = "X-Correlation-ID"
# Probes every few seconds would drown the log; they are logged at debug level.
_PROBES = {"/v1/health", "/v1/ready"}
_UNMATCHED = "unmatched"

log = logging.getLogger("txrisk.api")


class Observability:
    def __init__(self, app: ASGIApp, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope["method"]
        status = 500
        elapsed = stopwatch()
        with logs.scope(headers.get(CORRELATION_HEADER.lower())) as context:

            async def respond(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (CORRELATION_HEADER.lower().encode(), context.correlation_id.encode()),
                        ],
                    }
                await send(message)

            parent = None
            if TRACEPARENT in headers:
                parent = TraceContextTextMapPropagator().extract(
                    {TRACEPARENT: headers[TRACEPARENT]}
                )
            with span(
                f"{method} {scope['path']}", kind=trace.SpanKind.SERVER, context=parent
            ) as server:
                try:
                    await self.app(scope, receive, respond)
                finally:
                    route = getattr(scope.get("route"), "path", _UNMATCHED)
                    seconds = elapsed()
                    server.update_name(f"{method} {route}")
                    server.set_attribute("http.route", route)
                    server.set_attribute("http.status_code", status)
                    if status >= 500:
                        server.set_status(trace.Status(trace.StatusCode.ERROR))
                    self.metrics.http_requests.labels(method, route, str(status)).inc()
                    self.metrics.http_seconds.labels(method, route).observe(seconds)
                    log.log(
                        logging.DEBUG if scope["path"] in _PROBES else logging.INFO,
                        "%s %s -> %d",
                        method,
                        route,
                        status,
                        extra={"http_status": status, "duration_ms": round(seconds * 1000, 1)},
                    )
