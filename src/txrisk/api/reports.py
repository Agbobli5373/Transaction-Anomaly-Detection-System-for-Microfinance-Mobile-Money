"""Suspicious transaction reports, under `/v1/cases/{case_id}/report`.

An Analyst (the one assigned to the Case) or a Supervisor drafts a report from a Case closed as
fraud. Only a Supervisor approves it, and only a Supervisor exports it, and only once approved.
A report is read through the Case (`report` in `GET /v1/cases/{case_id}`). Drafting, approving
and exporting are all audited, the denied and failed attempts too; the entries name the report
and the Case, never the summary's text.
"""

from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from txrisk.api.audit import AuditedRoute, AuditorDep
from txrisk.api.auth import APPROVE_REPORTS, EXPORT_REPORTS, WORK_CASES, Principal, Role, require
from txrisk.api.cases import _refused
from txrisk.api.schemas import ReportDraftIn, ReportExport, ReportOut
from txrisk.store import cases, reports

_DENIED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No credential, or an invalid, expired or revoked one"},
    403: {"description": "The caller's roles do not permit this"},
}
_UNKNOWN: dict[int | str, dict[str, Any]] = {404: {"description": "No such Case, or no report"}}
_STATE: dict[int | str, dict[str, Any]] = {
    409: {
        "description": "The Case is not closed as fraud, or the report is not in a state for this"
    }
}

DRAFT_REPORT = AuditedRoute("report.draft", "case", "case_id")
APPROVE_REPORT = AuditedRoute("report.approve", "case", "case_id")
EXPORT_REPORT = AuditedRoute("report.export", "case", "case_id")


def add_report_routes(v1: APIRouter, engine: sa.Engine) -> None:
    @v1.post(
        "/cases/{case_id}/report",
        response_model=ReportOut,
        status_code=201,
        tags=["reports"],
        responses={**_DENIED, **_UNKNOWN, **_STATE},
    )
    def draft_report(
        case_id: int,
        body: ReportDraftIn,
        principal: Annotated[Principal, Depends(require(WORK_CASES, DRAFT_REPORT))],
        auditor: AuditorDep,
    ) -> ReportOut:
        """Draft the Suspicious transaction report of a Case closed as fraud, or rewrite its
        draft: the assigned Analyst or a Supervisor (403 for any other Analyst). 409 for a Case
        that is not closed as fraud, or whose report is already approved. Audited."""
        try:
            with engine.begin() as connection:
                report = reports.draft(
                    connection,
                    case_id,
                    summary=body.summary,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                    supervisor=Role.SUPERVISOR in principal.roles,
                )
                auditor.record(
                    connection,
                    principal,
                    DRAFT_REPORT.action,
                    object_type=DRAFT_REPORT.object_type,
                    object_id=str(case_id),
                    detail={"report_id": report.report_id, "length": len(body.summary)},
                )
                return ReportOut.of(report)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, DRAFT_REPORT, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except cases.NotPermitted as e:
            _refused(auditor, principal, DRAFT_REPORT, case_id, "denied", str(e))
            raise HTTPException(403, "not permitted to draft a report for this Case") from e
        except reports.ReportError as e:
            _refused(auditor, principal, DRAFT_REPORT, case_id, "failed", str(e))
            raise HTTPException(409, str(e)) from e

    @v1.post(
        "/cases/{case_id}/report/approve",
        response_model=ReportOut,
        tags=["reports"],
        responses={**_DENIED, **_UNKNOWN, **_STATE},
    )
    def approve_report(
        case_id: int,
        principal: Annotated[Principal, Depends(require(APPROVE_REPORTS, APPROVE_REPORT))],
        auditor: AuditorDep,
    ) -> ReportOut:
        """Approve a Case's drafted report. Supervisors only: an Analyst, an Admin or an API key
        gets 403. Approval is final, and makes the report exportable. Audited."""
        try:
            with engine.begin() as connection:
                report = reports.approve(
                    connection, case_id, actor_id=principal.subject, actor_name=principal.name
                )
                auditor.record(
                    connection,
                    principal,
                    APPROVE_REPORT.action,
                    object_type=APPROVE_REPORT.object_type,
                    object_id=str(case_id),
                    detail={"report_id": report.report_id},
                )
                return ReportOut.of(report)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, APPROVE_REPORT, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except reports.NoReport as e:
            _refused(auditor, principal, APPROVE_REPORT, case_id, "failed", str(e))
            raise HTTPException(404, "this Case has no report") from e
        except reports.ReportError as e:
            _refused(auditor, principal, APPROVE_REPORT, case_id, "failed", str(e))
            raise HTTPException(409, str(e)) from e

    @v1.get(
        "/cases/{case_id}/report/export",
        response_model=ReportExport,
        tags=["reports"],
        responses={
            **_DENIED,
            **_UNKNOWN,
            409: {"description": "The report is still a draft"},
            200: {
                "description": "The report as a JSON file download",
                "headers": {"Content-Disposition": {"schema": {"type": "string"}}},
            },
        },
    )
    def export_report(
        case_id: int,
        principal: Annotated[Principal, Depends(require(EXPORT_REPORTS, EXPORT_REPORT))],
        auditor: AuditorDep,
    ) -> JSONResponse:
        """The approved Suspicious transaction report as a file: the Case's Transactions with
        their Reasons, its notes and the summary, with Wallets by pseudonymised ID and phone
        numbers masked. Supervisors only, and only once approved (409 for a draft). Audited; the
        system never files the report."""
        try:
            with engine.begin() as connection:
                file = reports.report_file(connection, case_id)
                auditor.record(
                    connection,
                    principal,
                    EXPORT_REPORT.action,
                    object_type=EXPORT_REPORT.object_type,
                    object_id=str(case_id),
                    detail={
                        "report_id": file.report.report_id,
                        "transactions": len(file.transactions),
                    },
                )
                exported = ReportExport.of(file)
        except cases.CaseNotFound as e:
            _refused(auditor, principal, EXPORT_REPORT, case_id, "failed", "no such Case")
            raise HTTPException(404, "no such Case") from e
        except reports.NoReport as e:
            _refused(auditor, principal, EXPORT_REPORT, case_id, "failed", str(e))
            raise HTTPException(404, "this Case has no report") from e
        except reports.NotApproved as e:
            _refused(auditor, principal, EXPORT_REPORT, case_id, "failed", str(e))
            raise HTTPException(409, str(e)) from e
        name = f"suspicious-transaction-report-case-{case_id}.json"
        return JSONResponse(
            exported.model_dump(mode="json"),
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
