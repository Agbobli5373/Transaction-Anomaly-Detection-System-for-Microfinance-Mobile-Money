"""Structured JSON logs, each line carrying the `transaction_id` and `correlation_id` of the
scoring request or event it belongs to.

A `scope` opens a mutable context for one request or event; every log line inside it, from any
thread the work is handed to, reads its ids from that context. The ids are bound as they become
known (the correlation id first, the Transaction ID once the body is decoded).

Nothing secret or personal may be logged: call sites never pass tokens, API keys or MSISDNs, and a
scrubber over every message and exception text masks bearer tokens, key-like assignments and
anything shaped like an MSISDN as a safety net.
"""

import contextvars
import json
import logging
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

# Callers' correlation ids are echoed into logs and headers, so only a plain token is accepted.
_CORRELATION_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_SECRETS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (
        re.compile(
            r"(?i)\b(x-api-key|api[_-]?key|authorization|password|secret|token)(\s*[=:]\s*)\S+"
        ),
        r"\1\2[redacted]",
    ),
    # An MSISDN (233XXXXXXXXX, +233..., 0XXXXXXXXX): a run of nine or more digits.
    (re.compile(r"(?<![\w.])\+?\d[\d \-]{7,}\d(?![\w.])"), "[redacted-number]"),
]
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def scrub(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class LogContext:
    """The ids of the request or event being handled."""

    def __init__(self, correlation_id: str, transaction_id: str | None = None) -> None:
        self.correlation_id = correlation_id
        self.transaction_id = transaction_id


_current: contextvars.ContextVar[LogContext | None] = contextvars.ContextVar(
    "txrisk_log_context", default=None
)


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def accepted_correlation_id(value: str | None) -> str:
    """The caller's correlation id if it is a plain token, else a new one."""
    if value is not None and _CORRELATION_ID.fullmatch(value):
        return value
    return new_correlation_id()


@contextmanager
def scope(
    correlation_id: str | None = None, transaction_id: str | None = None
) -> Iterator[LogContext]:
    """Log everything inside under these ids. Without a usable correlation id, one is made."""
    context = LogContext(accepted_correlation_id(correlation_id), transaction_id)
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


def bind_transaction(transaction_id: str) -> None:
    """Attach the Transaction ID to the log lines of the request or event under way."""
    context = _current.get()
    if context is not None:
        context.transaction_id = transaction_id


def current_correlation_id() -> str | None:
    context = _current.get()
    return None if context is None else context.correlation_id


class JsonFormatter(logging.Formatter):
    """One JSON object per line: time, level, logger, message, the ids, the trace and any
    `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        context = _current.get()
        entry["correlation_id"] = None if context is None else context.correlation_id
        entry["transaction_id"] = None if context is None else context.transaction_id
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
            entry["span_id"] = format(span.span_id, "016x")
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in entry:
                entry[key] = scrub(value) if isinstance(value, str) else value
        if record.exc_info:
            entry["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log line to stderr as JSON, replacing any handler on the root logger (uvicorn's
    own loggers propagate to it)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
