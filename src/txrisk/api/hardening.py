"""What the API does to resist misuse, whoever calls: rate limits per API key, a request size
limit, strict CORS and security headers on every response."""

import json
import math
import threading
from collections.abc import Callable, Sequence
from datetime import datetime

from fastapi import FastAPI, HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Every response but the interactive docs: nothing on an API response may load or frame anything.
_API_CSP = "default-src 'none'; frame-ancestors 'none'"
# The interactive docs at /v1/docs load Swagger UI from jsDelivr and start it with an inline script.
_DOCS_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src https://cdn.jsdelivr.net; img-src data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'"
)
_SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
    (b"cache-control", b"no-store"),
]


class RateLimiter:
    """A token bucket per API key: up to a minute's allowance in a burst, refilled evenly.

    Buckets live in this process, so each API replica limits on its own; behind several replicas
    a key's effective limit is that many times its own.
    """

    def __init__(self, wall_clock: Callable[[], datetime]) -> None:
        self._wall_clock = wall_clock
        # key ID -> (requests allowed now, when they were counted, in seconds)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key_id: str, per_minute: int) -> float | None:
        """Spend one request; None if allowed, otherwise the seconds until one is."""
        now = self._wall_clock().timestamp()
        rate = per_minute / 60.0
        with self._lock:
            allowance, then = self._buckets.get(key_id, (float(per_minute), now))
            allowance = min(float(per_minute), allowance + max(0.0, now - then) * rate)
            if allowance < 1.0:
                self._buckets[key_id] = (allowance, now)
                return (1.0 - allowance) / rate
            self._buckets[key_id] = (allowance - 1.0, now)
            return None


class _TooLarge(HTTPException):
    def __init__(self, limit: int) -> None:
        super().__init__(413, f"request body over {limit} bytes")


class BodySizeLimit:
    """Rejects a request body over `max_bytes` with 413: at once when its Content-Length says so,
    and as soon as a body sent without one passes the limit."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            if not declared.isdigit():
                await _reply(send, 400, "invalid Content-Length")
                return
            if int(declared) > self.max_bytes:
                await _reply(send, 413, f"request body over {self.max_bytes} bytes")
                return
        received = 0

        async def limited() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI passes an HTTPException raised while reading the body through.
                    raise _TooLarge(self.max_bytes)
            return message

        await self.app(scope, limited, send)


async def _reply(send: Send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeaders:
    """Adds the security headers to every response, the API's own errors included."""

    def __init__(self, app: ASGIApp, docs_path: str) -> None:
        self.app = app
        self.docs_path = docs_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = _DOCS_CSP if scope["path"] == self.docs_path else _API_CSP

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [
                    (name, value)
                    for name, value in [
                        *_SECURITY_HEADERS,
                        (b"content-security-policy", csp.encode()),
                    ]
                    if name not in present
                ]
                message = {**message, "headers": [*message.get("headers", []), *extra]}
            await send(message)

        await self.app(scope, receive, with_headers)


def harden(
    app: FastAPI,
    *,
    cors_origins: Sequence[str],
    allowed_headers: Sequence[str],
    exposed_headers: Sequence[str],
    max_request_bytes: int,
) -> None:
    """Add the size limit, CORS and security headers, outermost last so every response, a
    rejected one too, carries the headers."""
    app.add_middleware(BodySizeLimit, max_bytes=max_request_bytes)
    # Only the configured origins, and no cookies: browsers send the bearer token themselves.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cors_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=list(allowed_headers),
        expose_headers=list(exposed_headers),
        allow_credentials=False,
        max_age=600,
    )
    app.add_middleware(SecurityHeaders, docs_path=app.docs_url or "")


def retry_after(seconds: float) -> str:
    """A Retry-After value: whole seconds, never zero."""
    return str(max(1, math.ceil(seconds)))
