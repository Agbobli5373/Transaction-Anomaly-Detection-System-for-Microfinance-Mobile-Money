"""Writing the audit log from API requests: the one helper every sensitive endpoint uses.

An endpoint takes the `Auditor` as a dependency and records what it did in the same transaction
as the action, so an action that cannot be audited does not happen:

    @router.get("/transactions/{transaction_id}")
    def view(
        transaction_id: str,
        principal: Annotated[Principal, Depends(require(READ, VIEWED))],
        auditor: AuditorDep,
    ) -> ...:
        with engine.begin() as connection:
            ...
            auditor.record(
                connection, principal, "transaction.view",
                object_type="transaction", object_id=transaction_id,
            )

A denied attempt (a 403) is recorded by `require` itself when the route names an `AuditedRoute`,
so a new sensitive endpoint audits both outcomes by declaring its action once. Cases, model
Promotion and report export use the same two pieces.

Scoring (`POST /v1/score`) is deliberately not audited: it is a high-volume service call that
reads nothing sensitive back. A 401 is not audited either: there is no caller to name, and an
unauthenticated flood would only fill the log.

The time is the operational wall clock at the request: an API request has no event time (ADR 0005
keeps scoring off the wall clock; an audit trail is about real time).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

import sqlalchemy as sa
from fastapi import Depends, Request

from txrisk.store import audit
from txrisk.store.audit import Actor, AuditEntry, Outcome

if TYPE_CHECKING:
    from txrisk.api.auth import Principal


@dataclass(frozen=True)
class AuditedRoute:
    """What a route is called in the audit log, for the entry `require` writes when it refuses a
    caller."""

    action: str
    object_type: str | None = None
    # The path parameter naming the object, if the route has one.
    object_param: str | None = None


def actor_of(principal: "Principal") -> Actor:
    return Actor(
        type=principal.kind,
        id=principal.subject,
        name=principal.name,
        roles=tuple(sorted(role.value for role in principal.roles)),
    )


@dataclass(frozen=True)
class Auditor:
    engine: sa.Engine
    # The real time (see the module docstring).
    wall_clock: Callable[[], datetime]

    def record(
        self,
        connection: sa.Connection,
        principal: "Principal",
        action: str,
        *,
        object_type: str | None = None,
        object_id: str | None = None,
        outcome: Outcome = "success",
        detail: Mapping[str, Any] | None = None,
    ) -> int:
        """Append an entry in `connection`'s transaction, so it commits with the action."""
        return audit.record(
            connection,
            AuditEntry(
                occurred_at=self.wall_clock(),
                actor=actor_of(principal),
                action=action,
                outcome=outcome,
                object_type=object_type,
                object_id=object_id,
                detail={} if detail is None else detail,
            ),
        )

    def record_alone(self, principal: "Principal", action: str, **entry: Any) -> int:
        """Append an entry in a transaction of its own: for an outcome whose action was rolled
        back or never started (a refusal, an unknown object)."""
        with self.engine.begin() as connection:
            return self.record(connection, principal, action, **entry)


def auditor(request: Request) -> Auditor:
    found: Auditor = request.app.state.auditor
    return found


AuditorDep = Annotated[Auditor, Depends(auditor)]
