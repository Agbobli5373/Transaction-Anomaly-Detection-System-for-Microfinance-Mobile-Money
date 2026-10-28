"""The audit log: an append-only record of who did what to which object, and how it ended.

Every sensitive action writes one `AuditEntry` through `record`; the API's helper in
`txrisk.api.audit` does it for requests, and later features (Cases, model Promotion, report
export) add their own actions the same way. Nothing here updates or deletes: the application's
database role cannot (see `txrisk.store.roles`).

An entry never holds a secret. Callers pass identifiers and small facts only, and an object ID a
caller merely typed (one that is not known to exist) is kept out if it looks like a phone number,
because a raw MSISDN must never be stored.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa

from txrisk.store.tables import audit_log

ActorType = Literal["person", "service", "operator"]
Outcome = Literal["success", "denied", "failed"]

REDACTED = "[redacted]"
_SAFE_ID = re.compile(r"[A-Za-z0-9._:@-]{1,128}")
# A phone number, with or without the country prefix and a leading plus.
_MSISDN_SHAPE = re.compile(r"\+?\d{9,15}")


@dataclass(frozen=True)
class Actor:
    type: ActorType
    # Keycloak's subject ID, an API key ID, or the command an operator ran.
    id: str
    # The username or API key name; for reading, not matching.
    name: str
    roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditEntry:
    # The event time of what happened, or for a request with none, the operational wall clock.
    occurred_at: datetime
    actor: Actor
    # A stable dotted code, e.g. "transaction.view".
    action: str
    outcome: Outcome = "success"
    object_type: str | None = None
    # Left out for a listing, which names no one object.
    object_id: str | None = None
    # Facts safe to keep for good. Never a secret.
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AuditRecord:
    audit_id: int
    occurred_at: datetime
    actor_type: str
    actor_id: str
    actor_name: str
    actor_roles: tuple[str, ...]
    action: str
    object_type: str | None
    object_id: str | None
    outcome: str
    detail: dict[str, Any]


def _storable_id(object_id: str | None, outcome: Outcome) -> str | None:
    if object_id is None:
        return None
    if _SAFE_ID.fullmatch(object_id) is None:
        return REDACTED
    # A successful action names an object that exists, so its ID is one the system stores. A
    # denied or failed one may carry whatever the caller typed.
    if outcome != "success" and _MSISDN_SHAPE.fullmatch(object_id) is not None:
        return REDACTED
    return object_id


def record(connection: sa.Connection, entry: AuditEntry) -> int:
    """Append `entry` in the connection's transaction and return its audit ID.

    Passing the transaction of the action being recorded makes the two commit or roll back
    together: a change that cannot be audited does not happen.
    """
    audit_id = connection.execute(
        sa.insert(audit_log)
        .values(
            occurred_at=entry.occurred_at,
            actor_type=entry.actor.type,
            actor_id=entry.actor.id,
            actor_name=entry.actor.name,
            actor_roles=list(entry.actor.roles),
            action=entry.action,
            object_type=entry.object_type,
            object_id=_storable_id(entry.object_id, entry.outcome),
            outcome=entry.outcome,
            detail=dict(entry.detail),
        )
        .returning(audit_log.c.audit_id)
    ).scalar_one()
    return int(audit_id)


def query(
    connection: sa.Connection,
    *,
    actor_id: str | None = None,
    actor_type: str | None = None,
    action: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    outcome: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> list[AuditRecord]:
    """Entries newest first, matching every filter given. `since` is inclusive, `until`
    exclusive; `before_id` continues after the last entry of a previous page."""
    filters = {
        audit_log.c.actor_id: actor_id,
        audit_log.c.actor_type: actor_type,
        audit_log.c.action: action,
        audit_log.c.object_type: object_type,
        audit_log.c.object_id: object_id,
        audit_log.c.outcome: outcome,
    }
    statement = sa.select(audit_log).order_by(audit_log.c.audit_id.desc()).limit(limit)
    for column, wanted in filters.items():
        if wanted is not None:
            statement = statement.where(column == wanted)
    if since is not None:
        statement = statement.where(audit_log.c.occurred_at >= since)
    if until is not None:
        statement = statement.where(audit_log.c.occurred_at < until)
    if before_id is not None:
        statement = statement.where(audit_log.c.audit_id < before_id)
    return [
        AuditRecord(
            audit_id=row.audit_id,
            occurred_at=row.occurred_at,
            actor_type=row.actor_type,
            actor_id=row.actor_id,
            actor_name=row.actor_name,
            actor_roles=tuple(row.actor_roles),
            action=row.action,
            object_type=row.object_type,
            object_id=row.object_id,
            outcome=row.outcome,
            detail=row.detail,
        )
        for row in connection.execute(statement)
    ]
