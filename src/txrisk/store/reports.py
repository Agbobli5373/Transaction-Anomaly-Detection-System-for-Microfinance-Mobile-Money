"""Suspicious transaction reports: drafting one from a Case closed as fraud, approving it and
reading what its export file holds.

A report belongs to one Case, which must be closed as fraud. An Analyst drafts it (the assigned
Analyst, or a Supervisor); drafting again rewrites the summary until a Supervisor approves, after
which the report is final (a database trigger refuses any change). Only an approved report can be
exported. The system produces the file and never files it (ADR 0003).

Every function takes the caller's connection, so the API commits an action with its audit entry.
Times are event times (ADR 0005), taken from the same reading a Case action uses.

The export file is read from the closed Case, which can no longer change, so it needs no copy of
its own. It names Wallets only by their pseudonymised IDs. People type free text, though, so notes
and the summary have anything shaped like a phone number masked on the way out.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store import cases
from txrisk.store.history import select_transactions, transaction_from_row
from txrisk.store.tables import alerts, suspicious_transaction_reports, transactions

ReportStatus = Literal["draft", "approved"]

# A phone number as free text carries it: nine to fifteen digits standing alone, or groups of
# digits split by spaces or dashes. Wallet IDs, Transaction IDs and timestamps do not match.
_PHONE_SHAPE = re.compile(r"(?<![\w.:-])\+?(?:\d{9,15}|\d{2,4}(?:[ -]\d{3,4}){2,3})(?![\w:-]|\.\d)")

_reports = suspicious_transaction_reports


def mask_phone_numbers(text: str) -> str:
    """`text` with each phone number shown only by its last three digits."""

    def masked(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group())
        return "*" * max(len(digits) - 3, 0) + digits[-3:]

    return _PHONE_SHAPE.sub(masked, text)


class ReportError(Exception):
    """Base of the reasons a report action cannot happen."""


class NotClosedAsFraud(ReportError):
    """Only a Case closed as fraud has a report."""


class ReportApproved(ReportError):
    """The report is approved, so it is final."""


class NoReport(ReportError):
    """The Case has no report yet."""


class NotApproved(ReportError):
    """The report is still a draft, so it cannot be exported."""


@dataclass(frozen=True)
class Report:
    report_id: int
    case_id: int
    status: ReportStatus
    summary: str
    drafted_at: datetime
    drafted_by_name: str
    approved_at: datetime | None
    approved_by_name: str | None


@dataclass(frozen=True)
class ReportFile:
    """What an approved report's export file is made of, before masking and formatting."""

    report: Report
    case: cases.CaseSummary
    # The Transactions that were Alerted on the Case, oldest first, each with its assessment.
    transactions: list[tuple[Transaction, RiskAssessment]]
    # The Case's notes, and the comments on its moves, oldest first.
    notes: list[cases.CaseEvent]


_COLUMNS = (
    _reports.c.report_id,
    _reports.c.case_id,
    _reports.c.status,
    _reports.c.summary,
    _reports.c.drafted_at,
    _reports.c.drafted_by_name,
    _reports.c.approved_at,
    _reports.c.approved_by_name,
)


def get_report(connection: sa.Connection, case_id: int) -> Report | None:
    row = connection.execute(
        sa.select(*_COLUMNS).where(_reports.c.case_id == case_id)
    ).one_or_none()
    return None if row is None else Report(**row._mapping)


def draft(
    connection: sa.Connection,
    case_id: int,
    *,
    summary: str,
    actor_id: str,
    actor_name: str,
    supervisor: bool,
) -> Report:
    """Draft the Case's report, or rewrite its draft, as the assigned Analyst or a Supervisor.

    Raises `CaseNotFound`, `NotPermitted`, `NotClosedAsFraud` or `ReportApproved`.
    """
    locked = cases.lock(connection, case_id)
    cases.check_may_work(locked.assignee_id, actor_id=actor_id, supervisor=supervisor)
    if locked.status != "closed_fraud":
        raise NotClosedAsFraud(f"a Case that is {locked.status} cannot have a report")
    values = {
        "summary": summary,
        "drafted_at": cases.event_time(connection, case_id),
        "drafted_by_id": actor_id,
        "drafted_by_name": actor_name,
    }
    # The Case row is locked, so two drafts of one Case run one after the other.
    existing = get_report(connection, case_id)
    if existing is None:
        connection.execute(insert(_reports).values(case_id=case_id, status="draft", **values))
    elif existing.status == "approved":
        raise ReportApproved(f"the report of Case {case_id} is approved")
    else:
        connection.execute(
            sa.update(_reports).where(_reports.c.case_id == case_id).values(**values)
        )
    report = get_report(connection, case_id)
    assert report is not None
    return report


def approve(connection: sa.Connection, case_id: int, *, actor_id: str, actor_name: str) -> Report:
    """Approve the Case's draft. The caller has checked the actor is a Supervisor.

    Raises `CaseNotFound`, `NoReport` or `ReportApproved`.
    """
    cases.lock(connection, case_id)
    report = get_report(connection, case_id)
    if report is None:
        raise NoReport(f"Case {case_id} has no report")
    if report.status == "approved":
        raise ReportApproved(f"the report of Case {case_id} is already approved")
    connection.execute(
        sa.update(_reports)
        .where(_reports.c.case_id == case_id)
        .values(
            status="approved",
            approved_at=cases.event_time(connection, case_id),
            approved_by_id=actor_id,
            approved_by_name=actor_name,
        )
    )
    approved = get_report(connection, case_id)
    assert approved is not None
    return approved


def report_file(connection: sa.Connection, case_id: int) -> ReportFile:
    """What the export of the Case's approved report holds.

    Raises `CaseNotFound`, `NoReport` or `NotApproved`.
    """
    case = cases.get_case(connection, case_id)
    if case is None:
        raise cases.CaseNotFound(case_id)
    report = get_report(connection, case_id)
    if report is None:
        raise NoReport(f"Case {case_id} has no report")
    if report.status != "approved":
        raise NotApproved(f"the report of Case {case_id} is a draft until a Supervisor approves it")
    scored = {a.transaction_id: a.assessment for a in cases.case_alerts(connection, case_id)}
    rows = connection.execute(
        select_transactions()
        .join(alerts, alerts.c.transaction_id == transactions.c.transaction_id)
        .where(alerts.c.case_id == case_id)
        .order_by(transactions.c.occurred_at, transactions.c.transaction_id)
    )
    return ReportFile(
        report=report,
        case=case,
        transactions=[
            (t, scored[t.transaction_id]) for t in (transaction_from_row(row) for row in rows)
        ],
        notes=[
            e
            for e in cases.events(connection, case_id)
            if e.body is not None and e.kind in ("note", "transition")
        ],
    )
