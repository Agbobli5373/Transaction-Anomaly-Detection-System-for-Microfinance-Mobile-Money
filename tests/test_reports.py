"""Suspicious transaction reports: drafting only from a Case closed as fraud, approval by a
Supervisor alone, export only once approved, what the file holds, and the audit of all three.
Against real Postgres, through the same desk of people the Cases tests use."""

import json
from typing import Any

import pytest
import sqlalchemy as sa
import test_cases
from scoring_helpers import HOUR, NOW, SUBJECT
from test_cases import FRIEND, OTHER, PHONE_SHAPE, STRANGER, Desk, close_as_fraud

from txrisk.store.reports import mask_phone_numbers
from txrisk.store.roles import GRANTS

# The same desk of Analysts (ama, yaw), a Supervisor (esi), an Admin and a service.
desk = test_cases.desk

SUMMARY = "Takeover after a SIM swap; funds cashed out through two Agents within the hour."


def report_path(case_id: int, tail: str = "") -> str:
    return f"/v1/cases/{case_id}/report{tail}"


def draft(desk: Desk, case_id: int, who: dict[str, str] | None = None, text: str = SUMMARY) -> Any:
    return desk.post(report_path(case_id), who or desk.ama, {"summary": text})


def approve(desk: Desk, case_id: int, who: dict[str, str] | None = None) -> Any:
    return desk.post(report_path(case_id, "/approve"), who or desk.esi, {})


def export(desk: Desk, case_id: int, who: dict[str, str] | None = None) -> Any:
    return desk.get(report_path(case_id, "/export"), who or desk.esi)


def fraud_case(desk: Desk) -> int:
    one, two = desk.alert(NOW), desk.alert(NOW + HOUR)
    desk.clock.advance(NOW + 2 * HOUR)
    return close_as_fraud(desk, one, two)


def report_of(desk: Desk, case_id: int) -> dict[str, Any] | None:
    found: dict[str, Any] | None = desk.get(f"/v1/cases/{case_id}", desk.esi).json()["report"]
    return found


# --- drafting ------------------------------------------------------------------------------


def fraud_case_on(desk: Desk, wallet: str) -> int:
    alert = desk.alert(NOW, wallet=wallet)
    desk.clock.advance(NOW + HOUR)
    return close_as_fraud(desk, alert)


def test_a_report_can_be_drafted_only_from_a_case_closed_as_fraud(desk: Desk):
    open_case = desk.case_of(desk.alert(wallet=SUBJECT))
    investigating = desk.case_of(desk.alert(wallet=OTHER))
    legitimate = desk.case_of(desk.alert(wallet=FRIEND))
    for case_id in (investigating, legitimate):
        desk.assign(case_id)
        desk.move(case_id, "investigating")
    desk.move(legitimate, "closed_legitimate")
    fraud = fraud_case_on(desk, STRANGER)

    refused = [draft(desk, c, desk.esi) for c in (open_case, investigating, legitimate)]

    assert [r.status_code for r in refused] == [409, 409, 409]
    assert all(report_of(desk, c) is None for c in (open_case, investigating, legitimate))
    made = draft(desk, fraud, desk.esi)
    assert made.status_code == 201
    assert made.json()["status"] == "draft"
    assert made.json()["summary"] == SUMMARY
    assert made.json()["case_id"] == fraud


def test_an_unknown_case_has_no_report_to_draft_approve_or_export(desk: Desk):
    assert draft(desk, 999, desk.esi).status_code == 404
    assert approve(desk, 999).status_code == 404
    assert export(desk, 999).status_code == 404


def test_the_assigned_analyst_and_a_supervisor_draft_but_no_other_analyst_admin_or_service(
    desk: Desk,
):
    case_id = fraud_case(desk)  # assigned to ama

    assert draft(desk, case_id, desk.yaw).status_code == 403
    assert draft(desk, case_id, desk.admin).status_code == 403
    assert draft(desk, case_id, desk.service).status_code == 403
    assert report_of(desk, case_id) is None

    assert draft(desk, case_id, desk.ama).status_code == 201
    rewritten = draft(desk, case_id, desk.esi, "A Supervisor's rewrite.")
    assert rewritten.status_code == 201
    assert rewritten.json()["summary"] == "A Supervisor's rewrite."
    assert rewritten.json()["drafted_by_name"] == "esi"


def test_a_draft_can_be_rewritten_until_it_is_approved_and_then_never(desk: Desk):
    case_id = fraud_case(desk)
    first = draft(desk, case_id).json()
    assert draft(desk, case_id, text="Second thoughts.").json()["report_id"] == first["report_id"]

    assert approve(desk, case_id).status_code == 200

    assert draft(desk, case_id, text="Too late.").status_code == 409
    assert report_of(desk, case_id)["summary"] == "Second thoughts."  # type: ignore[index]


def test_an_empty_summary_is_refused(desk: Desk):
    case_id = fraud_case(desk)

    assert draft(desk, case_id, text="").status_code == 422


# --- approval ------------------------------------------------------------------------------


def test_an_analyst_cannot_approve_a_report_not_even_their_own(desk: Desk):
    case_id = fraud_case(desk)
    draft(desk, case_id, desk.ama)

    for who in (desk.ama, desk.yaw, desk.admin, desk.service):
        assert approve(desk, case_id, who).status_code == 403

    assert report_of(desk, case_id)["status"] == "draft"  # type: ignore[index]


def test_a_supervisor_approves_a_draft_once(desk: Desk):
    case_id = fraud_case(desk)
    draft(desk, case_id)

    approved = approve(desk, case_id)

    assert approved.status_code == 200
    body = approved.json()
    assert (body["status"], body["approved_by_name"], body["drafted_by_name"]) == (
        "approved",
        "esi",
        "ama",
    )
    assert body["approved_at"] is not None
    assert approve(desk, case_id).status_code == 409
    assert report_of(desk, case_id) == body


def test_a_case_without_a_draft_has_nothing_to_approve(desk: Desk):
    case_id = fraud_case(desk)

    assert approve(desk, case_id).status_code == 404


def test_the_database_refuses_any_change_to_an_approved_report(desk: Desk):
    case_id = fraud_case(desk)
    draft(desk, case_id)
    approve(desk, case_id)

    with pytest.raises(sa.exc.DBAPIError, match="cannot be changed"), desk.engine.begin() as c:
        c.execute(
            sa.text("UPDATE suspicious_transaction_reports SET summary = 'x' WHERE case_id = :c"),
            {"c": case_id},
        )


# --- export --------------------------------------------------------------------------------


def test_only_an_approved_report_can_be_exported(desk: Desk):
    case_id = fraud_case(desk)

    assert export(desk, case_id).status_code == 404  # no report yet
    draft(desk, case_id)
    assert export(desk, case_id).status_code == 409  # a draft
    approve(desk, case_id)
    assert export(desk, case_id).status_code == 200


def test_only_a_supervisor_can_export_even_an_approved_report(desk: Desk):
    case_id = fraud_case(desk)
    draft(desk, case_id)
    approve(desk, case_id)

    for who in (desk.ama, desk.yaw, desk.admin, desk.service):
        assert export(desk, case_id, who).status_code == 403


def test_the_export_file_holds_the_cases_transactions_reasons_and_notes(desk: Desk):
    one, two = desk.alert(NOW, score=91.0), desk.alert(NOW + HOUR, score=84.0)
    quiet = desk.medium(NOW + 2 * HOUR)  # not alerted: not part of the report
    desk.clock.advance(NOW + 3 * HOUR)
    case_id = desk.case_of(one)
    desk.assign(case_id)
    desk.move(case_id, "investigating")
    desk.post(f"/v1/cases/{case_id}/notes", desk.ama, {"text": "Owner unreachable."})
    desk.post(
        f"/v1/cases/{case_id}/transition", desk.ama, {"to": "closed_fraud", "comment": "Confirmed."}
    )
    draft(desk, case_id)
    approve(desk, case_id)

    response = export(desk, case_id)

    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    assert f"case-{case_id}" in response.headers["content-disposition"]
    file = response.json()
    assert file["case"]["case_id"] == case_id
    assert file["summary"] == SUMMARY
    assert (file["drafted_by"], file["approved_by"]) == ("ama", "esi")
    shown = [t["transaction"]["transaction_id"] for t in file["transactions"]]
    assert shown == [one.transaction_id, two.transaction_id]
    assert quiet.transaction_id not in response.text
    first = file["transactions"][0]
    assert first["transaction"]["subject_wallet_id"] == one.subject_wallet_id
    assert first["transaction"]["amount"] == one.amount
    assert first["assessment"]["risk_score"] == 91.0
    assert first["assessment"]["reasons"][0]["code"] == "SIM_SWAP_DRAIN"
    assert [(n["kind"], n["author"], n["text"]) for n in file["notes"]] == [
        ("note", "ama", "Owner unreachable."),
        ("transition", "ama", "Confirmed."),
    ]


def test_the_export_file_carries_no_raw_phone_number_even_one_typed_into_a_note(desk: Desk):
    case_id = fraud_case(desk)  # assigned to ama
    # Closed Cases take no notes, so the number goes in the summary and in a move's comment.
    with desk.engine.begin() as connection:
        connection.execute(
            sa.text(
                "UPDATE case_events SET body = 'Rang 233 241 234 567 and +233241234567'"
                " WHERE case_id = :c AND to_status = 'closed_fraud'"
            ),
            {"c": case_id},
        )
    draft(desk, case_id, text="Victim 0241234567 reported it; sender 233201112223.")
    approve(desk, case_id)

    text = export(desk, case_id).text

    assert PHONE_SHAPE.search(text) is None, PHONE_SHAPE.search(text)
    assert "0241234567" not in text
    assert "241 234" not in text
    assert "*******567" in text
    assert "Victim" in text


def test_phone_numbers_are_masked_to_their_last_three_digits():
    assert mask_phone_numbers("call 0241234567 now") == "call *******567 now"
    assert mask_phone_numbers("+233 241 234 567") == "*********567"
    # Identifiers and times are not phone numbers.
    assert mask_phone_numbers("w-ab12cd34 at 2026-03-01T10:00:00Z, 1234.50 GHS") == (
        "w-ab12cd34 at 2026-03-01T10:00:00Z, 1234.50 GHS"
    )


# --- the audit -----------------------------------------------------------------------------


def test_drafting_approving_and_exporting_are_audited_with_who_and_which_Case(desk: Desk):
    case_id = fraud_case(desk)

    draft(desk, case_id, desk.ama, "Private findings about the victim.")
    approve(desk, case_id)
    export(desk, case_id)
    rows = [r for r in desk.audit() if r["action"].startswith("report.")]

    assert [(r["action"], r["actor_name"], r["outcome"]) for r in rows] == [
        ("report.draft", "ama", "success"),
        ("report.approve", "esi", "success"),
        ("report.export", "esi", "success"),
    ]
    assert {(r["object_type"], r["object_id"]) for r in rows} == {("case", str(case_id))}
    assert rows[0]["actor_roles"] == ["analyst"]
    assert rows[2]["detail"]["transactions"] == 2
    assert "Private findings" not in json.dumps([r["detail"] for r in rows])


def test_denied_and_failed_report_actions_are_audited_too(desk: Desk):
    open_case = desk.case_of(desk.alert())
    closed = fraud_case_on(desk, OTHER)
    draft(desk, closed, desk.ama)
    draft(desk, closed, desk.yaw)  # not their Case
    approve(desk, closed, desk.ama)  # an Analyst approving
    export(desk, closed)  # still a draft
    draft(desk, open_case, desk.esi)  # not closed as fraud
    export(desk, closed, desk.admin)

    rows = {(r["action"], r["actor_name"], r["outcome"]) for r in desk.audit()}

    assert {
        ("report.draft", "yaw", "denied"),
        ("report.approve", "ama", "denied"),
        ("report.export", "esi", "failed"),
        ("report.draft", "esi", "failed"),
        ("report.export", "kofi", "denied"),
    } <= rows


def test_an_export_that_cannot_be_audited_is_not_handed_over(desk: Desk):
    case_id = fraud_case(desk)
    draft(desk, case_id)
    approve(desk, case_id)
    with desk.engine.begin() as connection:
        connection.execute(
            sa.text("ALTER TABLE audit_log ADD CONSTRAINT refuse CHECK (action <> 'report.export')")
        )

    with pytest.raises(sa.exc.DBAPIError):
        export(desk, case_id)


def test_the_app_role_may_write_reports_but_never_delete_them():
    assert GRANTS["suspicious_transaction_reports"] == ("SELECT", "INSERT", "UPDATE")
