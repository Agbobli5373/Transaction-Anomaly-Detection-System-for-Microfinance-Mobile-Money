"""The Cases endpoints and the Medium watch list, under `/v1`.

Reads are for Analysts and Supervisors (no API key scope reaches them). An Analyst sees the Cases
assigned to them and the unassigned open ones; a Supervisor sees all. Only a Supervisor assigns.
Notes and transitions need the assigned Analyst or a Supervisor. Every action is audited, the
denied and failed attempts too, through the helper in `txrisk.api.audit`.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query

from txrisk.api.audit import AuditedRoute, Auditor, AuditorDep
from txrisk.api.auth import (
    ASSIGN_CASES,
    READ_CASES,
    READ_WATCHLIST,
    WORK_CASES,
    Principal,
    Role,
    require,
)
from txrisk.api.schemas import (
    AssessmentOut,
    AssignIn,
    CaseAlertOut,
    CaseDetail,
    CaseEventOut,
    CaseOut,
    CasePage,
    CaseStatusName,
    NoteIn,
    TransactionOut,
    TransactionType,
    TransactionView,
    TransitionIn,
    WatchlistItem,
    WatchlistPage,
)
from txrisk.store import cases, watchlist
from txrisk.store.audit import Outcome

_DENIED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No credential, or an invalid, expired or revoked one"},
    403: {"description": "The caller's roles do not permit this"},
}
_UNKNOWN: dict[int | str, dict[str, Any]] = {404: {"description": "No such Case"}}
_CLOSED: dict[int | str, dict[str, Any]] = {409: {"description": "The Case is closed"}}

LIST_CASES = AuditedRoute("case.list", "case")
VIEW_CASE = AuditedRoute("case.view", "case", "case_id")
ASSIGN_CASE = AuditedRoute("case.assign", "case", "case_id")
NOTE_CASE = AuditedRoute("case.note", "case", "case_id")
TRANSITION_CASE = AuditedRoute("case.transition", "case", "case_id")
LIST_WATCHLIST = AuditedRoute("watchlist.list", "watchlist")

# How many of the Subject wallet's latest Transactions a Case shows.
RECENT_TRANSACTIONS = 20


def _is_supervisor(principal: Principal) -> bool:
    return Role.SUPERVISOR in principal.roles


def _filters(**given: object) -> dict[str, object]:
    return {k: v for k, v in given.items() if v is not None}


def _cursor(at: datetime, transaction_id: str) -> str:
    raw = json.dumps([at.isoformat(), transaction_id]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _parse_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        at, transaction_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(at), str(transaction_id)
    except (binascii.Error, ValueError, TypeError) as e:
        raise HTTPException(422, "before is not a cursor this API returned") from e


def _detail(connection: sa.Connection, case: cases.CaseSummary) -> CaseDetail:
    outcome = cases.disposition(case.status)
    return CaseDetail(
        case=CaseOut.of(case),
        alerts=[
            CaseAlertOut(
                alert_id=a.alert_id,
                transaction_id=a.transaction_id,
                raised_at=a.raised_at,
                assessment=AssessmentOut.of(a.assessment),
                disposition=outcome,
            )
            for a in cases.case_alerts(connection, case.case_id)
        ],
        recent_transactions=[
            TransactionView(
                transaction=TransactionOut.of(transaction),
                assessment=None if assessment is None else AssessmentOut.of(assessment),
            )
            for transaction, assessment in cases.recent_transactions(
                connection, case.subject_wallet_id, limit=RECENT_TRANSACTIONS
            )
        ],
        events=[
            CaseEventOut.model_validate(e.__dict__) for e in cases.events(connection, case.case_id)
        ],
    )


def _refused(
    auditor: Auditor,
    principal: Principal,
    route: AuditedRoute,
    case_id: int,
    outcome: Outcome,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Audit an action that did not happen, in a transaction of its own (the action's rolled
    back)."""
    auditor.record_alone(
        principal,
        route.action,
        object_type=route.object_type,
        object_id=str(case_id),
        outcome=outcome,
        detail={"reason": reason, **(extra or {})},
    )


def add_case_routes(v1: APIRouter, engine: sa.Engine) -> None:
    @v1.get("/cases", response_model=CasePage, tags=["cases"], responses=_DENIED)
    def list_cases(
        principal: Annotated[Principal, Depends(require(READ_CASES, LIST_CASES))],
        auditor: AuditorDep,
        status: CaseStatusName | None = None,
        assignee: Annotated[
            str | None, Query(description="an Analyst's subject ID, or `me`")
        ] = None,
        wallet: Annotated[str | None, Query(description="the Subject wallet")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[int | None, Query(description="next_before of the previous page")] = None,
    ) -> CasePage:
        """Cases, newest first, matching every filter given. A Supervisor sees all; an Analyst
        the Cases assigned to them and the unassigned open ones. Audited."""
        assignee_id = principal.subject if assignee == "me" else assignee
        with engine.begin() as connection:
            found = cases.list_cases(
                connection,
                limit=limit,
                before_id=before,
                status=status,
                assignee_id=assignee_id,
                subject_wallet_id=wallet,
                visible_to_analyst=None if _is_supervisor(principal) else principal.subject,
            )
            auditor.record(
                connection,
                principal,
                LIST_CASES.action,
                object_type=LIST_CASES.object_type,
                detail={
                    "filters": _filters(
                        status=status, assignee=assignee, wallet=wallet, before=before
                    ),
                    "limit": limit,
                    "returned": len(found),
                },
            )
        return CasePage(
            cases=[CaseOut.of(c) for c in found],
            next_before=found[-1].case_id if len(found) == limit else None,
        )

    @v1.get(
        "/cases/{case_id}",
        response_model=CaseDetail,
        tags=["cases"],
        responses={**_DENIED, **_UNKNOWN},
    )
    def get_case(
        case_id: int,
        principal: Annotated[Principal, Depends(require(READ_CASES, VIEW_CASE))],
        auditor: AuditorDep,
    ) -> CaseDetail:
        """A Case with its Alerts (Risk score, Reasons and disposition), its notes and events,
        and the Subject wallet's recent Transactions. An Analyst sees only their own and the
        unassigned open ones (403 otherwise). Audited."""
        with engine.begin() as connection:
            case = cases.get_case(connection, case_id)
            permitted = case is not None and cases.may_view(
                case, person_id=principal.subject, supervisor=_is_supervisor(principal)
            )
            auditor.record(
                connection,
                principal,
                VIEW_CASE.action,
                object_type=VIEW_CASE.object_type,
                object_id=str(case_id),
                outcome="success" if permitted else ("failed" if case is None else "denied"),
                detail=(
                    None
                    if permitted
                    else {"reason": "no such Case" if case is None else "not assigned to them"}
                ),
            )
            detail = _detail(connection, case) if case is not None and permitted else None
        if case is None:
            raise HTTPException(404, "no such Case")
        if detail is None:
            raise HTTPException(403, "not permitted to read this Case")
        return detail

    @v1.post(
        "/cases/{case_id}/assign",
        response_model=CaseDetail,
        tags=["cases"],
        responses={**_DENIED, **_UNKNOWN, **_CLOSED},
    )
    def assign_case(
        case_id: int,
        assignment: AssignIn,
        principal: Annotated[Principal, Depends(require(ASSIGN_CASES, ASSIGN_CASE))],
        auditor: AuditorDep,
    ) -> CaseDetail:
        """Assign (or reassign) a Case to an Analyst. Supervisors only. Audited."""
        try:
            with engine.begin() as connection:
                cases.assign(
                    connection,
                    case_id,
                    assignee_id=assignment.assignee_id,
                    assignee_name=assignment.assignee_name,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                )
                auditor.record(
                    connection,
                    principal,
                    ASSIGN_CASE.action,
                    object_type=ASSIGN_CASE.object_type,
                    object_id=str(case_id),
                    detail={"assignee_id": assignment.assignee_id},
                )
                case = cases.get_case(connection, case_id)
                assert case is not None
                return _detail(connection, case)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, ASSIGN_CASE, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except cases.CaseClosed as e:
            _refused(auditor, principal, ASSIGN_CASE, case_id, "failed", "the Case is closed")
            raise HTTPException(409, "the Case is closed") from e

    @v1.post(
        "/cases/{case_id}/notes",
        response_model=CaseDetail,
        status_code=201,
        tags=["cases"],
        responses={**_DENIED, **_UNKNOWN, **_CLOSED},
    )
    def add_note(
        case_id: int,
        note: NoteIn,
        principal: Annotated[Principal, Depends(require(WORK_CASES, NOTE_CASE))],
        auditor: AuditorDep,
    ) -> CaseDetail:
        """Add a note to a Case: the assigned Analyst or a Supervisor (403 for any other
        Analyst). Audited, with the note's length rather than its text."""
        try:
            with engine.begin() as connection:
                cases.add_note(
                    connection,
                    case_id,
                    text=note.text,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                    supervisor=_is_supervisor(principal),
                )
                auditor.record(
                    connection,
                    principal,
                    NOTE_CASE.action,
                    object_type=NOTE_CASE.object_type,
                    object_id=str(case_id),
                    detail={"length": len(note.text)},
                )
                case = cases.get_case(connection, case_id)
                assert case is not None
                return _detail(connection, case)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, NOTE_CASE, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except cases.NotPermitted as e:
            _refused(auditor, principal, NOTE_CASE, case_id, "denied", str(e))
            raise HTTPException(403, "not permitted to add notes to this Case") from e
        except cases.CaseClosed as e:
            _refused(auditor, principal, NOTE_CASE, case_id, "failed", "the Case is closed")
            raise HTTPException(409, "the Case is closed") from e

    @v1.post(
        "/cases/{case_id}/transition",
        response_model=CaseDetail,
        tags=["cases"],
        responses={
            **_DENIED,
            **_UNKNOWN,
            409: {
                "description": "Not a valid move for the Case's status; a closed Case never reopens"
            },
        },
    )
    def transition_case(
        case_id: int,
        move: TransitionIn,
        principal: Annotated[Principal, Depends(require(WORK_CASES, TRANSITION_CASE))],
        auditor: AuditorDep,
    ) -> CaseDetail:
        """Move a Case: open to investigating, investigating to closed_fraud or
        closed_legitimate. The assigned Analyst or a Supervisor. A closed Case never reopens.
        Closing as fraud stores a Fraud label for each of the Case's Transactions and queues a
        `fraud.discovered` event for each, dated at the closing time (the simulation clock).
        Audited."""
        try:
            with engine.begin() as connection:
                moved = cases.transition(
                    connection,
                    case_id,
                    move.to,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                    supervisor=_is_supervisor(principal),
                    comment=move.comment,
                )
                auditor.record(
                    connection,
                    principal,
                    TRANSITION_CASE.action,
                    object_type=TRANSITION_CASE.object_type,
                    object_id=str(case_id),
                    detail={
                        "from": moved.previous,
                        "to": move.to,
                        "discovered": len(moved.discovered),
                    },
                )
                return _detail(connection, moved.case)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, TRANSITION_CASE, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except cases.NotPermitted as e:
            _refused(auditor, principal, TRANSITION_CASE, case_id, "denied", str(e))
            raise HTTPException(403, "not permitted to move this Case") from e
        except cases.InvalidTransition as e:
            _refused(
                auditor,
                principal,
                TRANSITION_CASE,
                case_id,
                "failed",
                str(e),
                {"from": e.status, "to": e.to},
            )
            raise HTTPException(409, str(e)) from e

    @v1.get("/watchlist", response_model=WatchlistPage, tags=["watch list"], responses=_DENIED)
    def get_watchlist(
        principal: Annotated[Principal, Depends(require(READ_WATCHLIST, LIST_WATCHLIST))],
        auditor: AuditorDep,
        wallet: Annotated[str | None, Query(description="the Subject wallet")] = None,
        region: str | None = None,
        transaction_type: TransactionType | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        before: Annotated[str | None, Query(description="next_before of the previous page")] = None,
    ) -> WatchlistPage:
        """Medium-band Transactions, newest first. They raise no Alert. For Analysts and
        Supervisors. Audited."""
        position = None if before is None else _parse_cursor(before)
        with engine.begin() as connection:
            found = watchlist.list_medium(
                connection,
                limit=limit,
                before=position,
                subject_wallet_id=wallet,
                region=region,
                transaction_type=transaction_type,
            )
            auditor.record(
                connection,
                principal,
                LIST_WATCHLIST.action,
                object_type=LIST_WATCHLIST.object_type,
                detail={
                    "filters": _filters(
                        wallet=wallet, region=region, transaction_type=transaction_type
                    ),
                    "limit": limit,
                    "returned": len(found),
                },
            )
        last = found[-1].transaction if len(found) == limit else None
        return WatchlistPage(
            items=[
                WatchlistItem(
                    transaction=TransactionOut.of(w.transaction),
                    assessment=AssessmentOut.of(w.assessment),
                )
                for w in found
            ],
            next_before=None if last is None else _cursor(last.occurred_at, last.transaction_id),
        )
