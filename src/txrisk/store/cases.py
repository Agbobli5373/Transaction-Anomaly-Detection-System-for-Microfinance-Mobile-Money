"""Cases: assigning them, noting them and moving them through open, investigating and closed.

An Alert joins its Subject wallet's active Case when it is raised (`records.raise_alert`); this
module is everything that happens to a Case after. Every function takes the caller's connection,
so the API commits an action together with its audit entry.

Times are event times (ADR 0005). A Case action has none of its own, so it happens at the
simulation clock's reading, or, if no event has moved the clock, at the latest Alert on the Case.

Closing a Case as fraud is a Fraud discovery for each Transaction that was Alerted on it: in the
same database transaction as the closure it stores their Fraud labels (source `case`) and writes
a `fraud.discovered` event per Transaction to the outbox, which `txrisk outbox` publishes. Nothing
is published inside the request, so a crash can neither lose a discovery nor announce one for a
closure that rolled back.

A Case closed as fraud keeps what a Suspicious transaction report needs: who closed it and when,
its Alerts with their Risk scores and Reasons, its notes and its events.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store.history import select_transactions, transaction_from_row
from txrisk.store.labels import insert_label
from txrisk.store.records import AlertRecord, assessment_from_row
from txrisk.store.tables import (
    alerts,
    case_events,
    cases,
    outbox,
    risk_assessments,
    simulation_clock,
    transactions,
)

CaseStatus = Literal["open", "investigating", "closed_fraud", "closed_legitimate"]
Disposition = Literal["confirmed_fraud", "dismissed"]

# The only moves: a Case is picked up, then closed one way or the other. Nothing leaves a closed
# Case, so it can never be reopened.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("investigating",),
    "investigating": ("closed_fraud", "closed_legitimate"),
    "closed_fraud": (),
    "closed_legitimate": (),
}
_CLOSED = ("closed_fraud", "closed_legitimate")
FRAUD_DISCOVERED = "fraud.discovered"
# The Fraud discovery channel of a Case closed as fraud, as `fraud.discovered` names it.
DISCOVERY_SOURCE: Literal["case"] = "case"


class CaseNotFound(LookupError):
    """No Case has this ID."""


class NotPermitted(Exception):
    """The caller is neither the Analyst assigned to the Case nor a Supervisor."""


class InvalidTransition(Exception):
    """The Case cannot move from its status to the one asked for."""

    def __init__(self, status: str, to: str) -> None:
        super().__init__(f"a Case that is {status} cannot become {to}")
        self.status = status
        self.to = to


class CaseClosed(Exception):
    """The Case is closed, so it can no longer be assigned or noted."""


def disposition(status: str) -> Disposition | None:
    """What a Case's outcome says of each of its Alerts; None until it is closed. Derived on
    read, never stored, so it cannot disagree with how the Case closed."""
    if status == "closed_fraud":
        return "confirmed_fraud"
    if status == "closed_legitimate":
        return "dismissed"
    return None


@dataclass(frozen=True)
class CaseSummary:
    case_id: int
    subject_wallet_id: str
    status: CaseStatus
    opened_at: datetime
    assignee_id: str | None
    assignee_name: str | None
    closed_at: datetime | None
    closed_by_name: str | None
    alert_count: int
    top_risk_score: float | None


@dataclass(frozen=True)
class CaseEvent:
    event_id: int
    kind: str
    occurred_at: datetime
    actor_id: str
    actor_name: str
    body: str | None
    from_status: str | None
    to_status: str | None
    assignee_id: str | None


@dataclass(frozen=True)
class Transitioned:
    """A Case after a transition, the status it left, and, for a closure as fraud, the
    Transactions discovered."""

    case: CaseSummary
    previous: str
    discovered: tuple[str, ...]


def _summaries() -> sa.Select[tuple[object, ...]]:
    counted = (
        sa.select(
            alerts.c.case_id,
            sa.func.count().label("alert_count"),
            sa.func.max(risk_assessments.c.risk_score).label("top_risk_score"),
        )
        .join(risk_assessments, risk_assessments.c.transaction_id == alerts.c.transaction_id)
        .group_by(alerts.c.case_id)
        .subquery()
    )
    return sa.select(
        cases.c.case_id,
        cases.c.subject_wallet_id,
        cases.c.status,
        cases.c.opened_at,
        cases.c.assignee_id,
        cases.c.assignee_name,
        cases.c.closed_at,
        cases.c.closed_by_name,
        sa.func.coalesce(counted.c.alert_count, 0).label("alert_count"),
        counted.c.top_risk_score,
    ).outerjoin(counted, counted.c.case_id == cases.c.case_id)


def _summary(row: sa.Row[tuple[object, ...]]) -> CaseSummary:
    return CaseSummary(**row._mapping)


def list_cases(
    connection: sa.Connection,
    *,
    limit: int,
    before_id: int | None = None,
    status: str | None = None,
    assignee_id: str | None = None,
    subject_wallet_id: str | None = None,
    visible_to_analyst: str | None = None,
) -> list[CaseSummary]:
    """Cases newest first matching every filter given; `before_id` continues a previous page.
    With `visible_to_analyst`, only what that Analyst may see: the Cases assigned to them and
    the unassigned open ones."""
    query = _summaries().order_by(cases.c.case_id.desc()).limit(limit)
    if before_id is not None:
        query = query.where(cases.c.case_id < before_id)
    if status is not None:
        query = query.where(cases.c.status == status)
    if assignee_id is not None:
        query = query.where(cases.c.assignee_id == assignee_id)
    if subject_wallet_id is not None:
        query = query.where(cases.c.subject_wallet_id == subject_wallet_id)
    if visible_to_analyst is not None:
        query = query.where(
            sa.or_(
                cases.c.assignee_id == visible_to_analyst,
                sa.and_(cases.c.assignee_id.is_(None), cases.c.status == "open"),
            )
        )
    return [_summary(row) for row in connection.execute(query)]


def get_case(connection: sa.Connection, case_id: int) -> CaseSummary | None:
    row = connection.execute(_summaries().where(cases.c.case_id == case_id)).one_or_none()
    return None if row is None else _summary(row)


def may_view(case: CaseSummary, *, person_id: str, supervisor: bool) -> bool:
    """Supervisors see every Case; an Analyst sees their own and the unassigned open ones."""
    if supervisor:
        return True
    return case.assignee_id == person_id or (case.assignee_id is None and case.status == "open")


def case_alerts(connection: sa.Connection, case_id: int) -> list[AlertRecord]:
    """The Case's Alerts, oldest first, with their Risk assessments."""
    rows = connection.execute(
        sa.select(
            alerts.c.alert_id,
            alerts.c.subject_wallet_id,
            alerts.c.raised_at,
            *risk_assessments.c,
        )
        .join(risk_assessments, risk_assessments.c.transaction_id == alerts.c.transaction_id)
        .where(alerts.c.case_id == case_id)
        .order_by(alerts.c.alert_id)
    )
    return [
        AlertRecord(
            alert_id=row.alert_id,
            transaction_id=row.transaction_id,
            subject_wallet_id=row.subject_wallet_id,
            raised_at=row.raised_at,
            assessment=assessment_from_row(row),
        )
        for row in rows
    ]


def recent_transactions(
    connection: sa.Connection, wallet_id: str, *, limit: int
) -> list[tuple[Transaction, RiskAssessment | None]]:
    """The Wallet's latest Transactions as Subject wallet, newest first, each with its Risk
    assessment if it was scored."""
    rows = connection.execute(
        select_transactions()
        .add_columns(*risk_assessments.c)
        .outerjoin(
            risk_assessments, risk_assessments.c.transaction_id == transactions.c.transaction_id
        )
        .where(transactions.c.subject_wallet_id == wallet_id)
        .order_by(transactions.c.occurred_at.desc(), transactions.c.transaction_id.desc())
        .limit(limit)
    )
    return [
        (
            transaction_from_row(row[:11]),
            None if row.risk_band is None else assessment_from_row(row),
        )
        for row in rows
    ]


def events(connection: sa.Connection, case_id: int) -> list[CaseEvent]:
    """What happened on the Case, oldest first."""
    rows = connection.execute(
        sa.select(case_events)
        .where(case_events.c.case_id == case_id)
        .order_by(case_events.c.event_id)
    )
    return [CaseEvent(**{k: v for k, v in row._mapping.items() if k != "case_id"}) for row in rows]


def event_time(connection: sa.Connection, case_id: int) -> datetime:
    """When an action on the Case happens: the simulation clock's reading, and never before the
    Case's latest Alert."""
    reading = connection.execute(sa.select(simulation_clock.c.at)).scalar_one_or_none()
    latest = connection.execute(
        sa.select(sa.func.max(alerts.c.raised_at)).where(alerts.c.case_id == case_id)
    ).scalar_one()
    moments: list[datetime] = [m for m in (reading, latest) if m is not None]
    if not moments:
        raise LookupError(f"Case {case_id} has no Alerts and the clock has no reading")
    return max(moments)


@dataclass(frozen=True)
class Locked:
    status: str
    assignee_id: str | None


def lock(connection: sa.Connection, case_id: int) -> Locked:
    """The Case's status and assignee, locked until the transaction ends, so concurrent
    actions on one Case happen one after the other."""
    row = connection.execute(
        sa.select(cases.c.status, cases.c.assignee_id)
        .where(cases.c.case_id == case_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise CaseNotFound(case_id)
    return Locked(row.status, row.assignee_id)


def _record_event(
    connection: sa.Connection,
    case_id: int,
    kind: str,
    *,
    at: datetime,
    actor_id: str,
    actor_name: str,
    body: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    assignee_id: str | None = None,
) -> None:
    connection.execute(
        insert(case_events).values(
            case_id=case_id,
            kind=kind,
            occurred_at=at,
            actor_id=actor_id,
            actor_name=actor_name,
            body=body,
            from_status=from_status,
            to_status=to_status,
            assignee_id=assignee_id,
        )
    )


def check_may_work(assignee_id: str | None, *, actor_id: str, supervisor: bool) -> None:
    if not supervisor and assignee_id != actor_id:
        raise NotPermitted("only the assigned Analyst or a Supervisor may work this Case")


def assign(
    connection: sa.Connection,
    case_id: int,
    *,
    assignee_id: str,
    assignee_name: str | None,
    actor_id: str,
    actor_name: str,
) -> None:
    """Assign (or reassign) the Case to an Analyst. The caller has checked the actor is a
    Supervisor. Raises `CaseNotFound`, or `CaseClosed` for a closed Case."""
    locked = lock(connection, case_id)
    if locked.status in _CLOSED:
        raise CaseClosed(case_id)
    at = event_time(connection, case_id)
    connection.execute(
        sa.update(cases)
        .where(cases.c.case_id == case_id)
        .values(assignee_id=assignee_id, assignee_name=assignee_name, assigned_by=actor_id)
    )
    _record_event(
        connection,
        case_id,
        "assigned",
        at=at,
        actor_id=actor_id,
        actor_name=actor_name,
        assignee_id=assignee_id,
    )


def add_note(
    connection: sa.Connection,
    case_id: int,
    *,
    text: str,
    actor_id: str,
    actor_name: str,
    supervisor: bool,
) -> None:
    """Add a note, as the assigned Analyst or a Supervisor. Raises `CaseNotFound`,
    `NotPermitted` or `CaseClosed`."""
    locked = lock(connection, case_id)
    check_may_work(locked.assignee_id, actor_id=actor_id, supervisor=supervisor)
    if locked.status in _CLOSED:
        raise CaseClosed(case_id)
    _record_event(
        connection,
        case_id,
        "note",
        at=event_time(connection, case_id),
        actor_id=actor_id,
        actor_name=actor_name,
        body=text,
    )


def transition(
    connection: sa.Connection,
    case_id: int,
    to: str,
    *,
    actor_id: str,
    actor_name: str,
    supervisor: bool,
    comment: str | None = None,
) -> Transitioned:
    """Move the Case to `to`, as the assigned Analyst or a Supervisor.

    Raises `CaseNotFound`, `NotPermitted`, or `InvalidTransition` (a closed Case included).
    Closing as fraud discovers the Case's Transactions at the closing time; see the module
    docstring.
    """
    locked = lock(connection, case_id)
    check_may_work(locked.assignee_id, actor_id=actor_id, supervisor=supervisor)
    if to not in TRANSITIONS.get(locked.status, ()):
        raise InvalidTransition(locked.status, to)
    at = event_time(connection, case_id)
    values: dict[str, object] = {"status": to}
    if to in _CLOSED:
        values |= {"closed_at": at, "closed_by_id": actor_id, "closed_by_name": actor_name}
    connection.execute(sa.update(cases).where(cases.c.case_id == case_id).values(**values))
    _record_event(
        connection,
        case_id,
        "transition",
        at=at,
        actor_id=actor_id,
        actor_name=actor_name,
        body=comment,
        from_status=locked.status,
        to_status=to,
    )
    discovered = _discover(connection, case_id, at) if to == "closed_fraud" else ()
    summary = get_case(connection, case_id)
    assert summary is not None
    return Transitioned(summary, locked.status, discovered)


def _discover(connection: sa.Connection, case_id: int, at: datetime) -> tuple[str, ...]:
    """A Fraud discovery at `at` for each Transaction alerted on the Case: its Fraud label, and
    its `fraud.discovered` event in the outbox. A Transaction already labelled (a complaint came
    first) keeps its first label and is still announced: consumers are idempotent."""
    # Imported here: the event contracts read the store's tables, and the events package pulls
    # in the workers, which import the store.
    from txrisk.events.contracts import FraudDiscovered, encode

    transaction_ids = tuple(
        connection.execute(
            sa.select(alerts.c.transaction_id)
            .where(alerts.c.case_id == case_id)
            .order_by(alerts.c.alert_id)
        ).scalars()
    )
    for transaction_id in transaction_ids:
        insert_label(connection, transaction_id, at, DISCOVERY_SOURCE)
        event = FraudDiscovered(
            transaction_id=transaction_id, discovered_at=at, source=DISCOVERY_SOURCE
        )
        connection.execute(
            insert(outbox).values(
                topic=FRAUD_DISCOVERED,
                key=transaction_id,
                payload=encode(event).decode(),
                published=False,
            )
        )
    return transaction_ids
