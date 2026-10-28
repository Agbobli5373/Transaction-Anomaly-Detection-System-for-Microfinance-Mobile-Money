"""The `/v1` request and response bodies."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    model_validator,
)

from txrisk.api.auth import SCOPES
from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store.api_keys import ApiKeyRecord
from txrisk.store.audit import AuditRecord
from txrisk.store.cases import CaseSummary
from txrisk.store.reports import Report, ReportFile, mask_phone_numbers
from txrisk.store.tables import COUNTERPARTY_COLUMNS

TransactionType = Literal["cash_in", "cash_out", "transfer", "payment", "bill_payment"]
_ID = Field(min_length=1, max_length=128)


def _known_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; one of {', '.join(SCOPES)}")
    return scope


# A scope an API key may carry.
Scope = Annotated[
    str, AfterValidator(_known_scope), WithJsonSchema({"type": "string", "enum": list(SCOPES)})
]


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TransactionIn(_Body):
    """A Transaction as the ledger reports it: exactly one Counterparty, of the kind its
    Transaction type deals with, and no balance after."""

    transaction_id: str = _ID
    # When it happened, with a UTC offset. It is the scoring clock (ADR 0005).
    occurred_at: AwareDatetime
    transaction_type: TransactionType
    # GHS, as Postgres stores money: an amount scored is exactly the amount stored, so the
    # features of later Transactions read it back unchanged.
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    subject_wallet_id: str = _ID
    direction: Literal["in", "out"]
    subject_balance_before: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    channel: Literal["ussd", "app"]
    device_id: str = _ID
    region: str = Field(min_length=1, max_length=64)
    counterparty_wallet_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    merchant_id: str | None = Field(default=None, min_length=1, max_length=128)
    biller_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _one_counterparty(self) -> Self:
        expected = COUNTERPARTY_COLUMNS[self.transaction_type]
        given = [
            c for c in sorted(set(COUNTERPARTY_COLUMNS.values())) if getattr(self, c) is not None
        ]
        if given != [expected]:
            raise ValueError(
                f"a {self.transaction_type} names exactly one Counterparty, in {expected}"
            )
        if (self.direction == "in") != (self.transaction_type == "cash_in"):
            raise ValueError("direction is in for a cash_in and out for every other type")
        return self

    def to_transaction(self) -> Transaction:
        return Transaction(
            transaction_id=self.transaction_id,
            occurred_at=self.occurred_at.astimezone(UTC),
            transaction_type=self.transaction_type,
            amount=float(self.amount),
            subject_wallet_id=self.subject_wallet_id,
            direction=self.direction,
            counterparty_id=getattr(self, COUNTERPARTY_COLUMNS[self.transaction_type]),
            region=self.region,
            subject_balance_before=float(self.subject_balance_before),
            device_id=self.device_id,
            channel=self.channel,
        )


class TransactionOut(_Body):
    transaction_id: str
    occurred_at: datetime
    transaction_type: str
    amount: float
    subject_wallet_id: str
    direction: str
    subject_balance_before: float
    channel: str
    device_id: str
    region: str
    counterparty_wallet_id: str | None = None
    agent_id: str | None = None
    merchant_id: str | None = None
    biller_id: str | None = None

    @classmethod
    def of(cls, transaction: Transaction) -> "TransactionOut":
        return cls(
            transaction_id=transaction.transaction_id,
            occurred_at=transaction.occurred_at,
            transaction_type=transaction.transaction_type,
            amount=transaction.amount,
            subject_wallet_id=transaction.subject_wallet_id,
            direction=transaction.direction,
            subject_balance_before=transaction.subject_balance_before,
            channel=transaction.channel,
            device_id=transaction.device_id,
            region=transaction.region,
            **{COUNTERPARTY_COLUMNS[transaction.transaction_type]: transaction.counterparty_id},
        )


class ReasonOut(_Body):
    # Stable, for filtering and counting.
    code: str
    message: str
    value: float | str | None


class AssessmentOut(_Body):
    transaction_id: str
    # 0-100.
    risk_score: float
    risk_band: Literal["Low", "Medium", "High"]
    # At most three, Critical rule hits first, then by contribution.
    reasons: list[ReasonOut]
    # rules, anomaly and supervised, each 0-1, or null without a model for it.
    component_scores: dict[str, float | None]
    model_version: str = Field(
        description=(
            "Which scale risk_score and risk_band are on. A bundle version: the stacked scale, "
            "a probability x 100, where High starts at that bundle's threshold (see "
            "/v1/models/active). The value `rules-only`: the rules scale, whole numbers 0-100 "
            "where High starts at 50 (`bands` in config/rules.toml) and a Critical rule hit is "
            "at least 90."
        )
    )
    degraded: bool = Field(
        description=(
            "True when the Champion model was unavailable, too slow or failed, and this was "
            "scored from Rule hits alone: model_version is then `rules-only` and the score is on "
            "the rules scale, not comparable with stacked scores. The full model re-scores it "
            "once healthy, replacing the stored assessment (a High keeps its Alert; a new High "
            "raises one)."
        )
    )

    @classmethod
    def of(cls, assessment: RiskAssessment) -> "AssessmentOut":
        return cls(
            transaction_id=assessment.transaction_id,
            risk_score=assessment.risk_score,
            risk_band=assessment.risk_band.value,
            reasons=[
                ReasonOut(code=r.code, message=r.message, value=r.value) for r in assessment.reasons
            ],
            component_scores=assessment.component_scores,
            model_version=assessment.model_version,
            degraded=assessment.degraded,
        )


class TransactionView(_Body):
    transaction: TransactionOut
    # Null for history that was loaded rather than scored.
    assessment: AssessmentOut | None


class AlertOut(_Body):
    alert_id: int
    transaction_id: str
    subject_wallet_id: str
    # The Transaction's event time.
    raised_at: datetime
    assessment: AssessmentOut


class AlertPage(_Body):
    alerts: list[AlertOut]
    # Pass as `before` for the next page; null on the last.
    next_before: int | None


class ActiveModel(_Body):
    model_version: str
    # Risk scores from this many up are Medium, and High.
    thresholds: dict[str, float]
    # The days each part of a bundle was fitted on; null for rules-only scoring.
    fitted_on: dict[str, object] | None


class Status(_Body):
    status: Literal["ok", "unavailable"]
    detail: str | None = None


class Readiness(Status):
    """Ready to score. `degraded` says whether it scores with the Champion model."""

    # True when the Champion model is unavailable, too slow or failing, so Transactions are
    # scored from Rule hits alone (Degraded mode). The service still answers: the status stays
    # `ok` and the HTTP status 200.
    degraded: bool = False
    reason: str | None = None
    # Transactions whose stored assessment is degraded and waits for the full model to re-score it.
    awaiting_rescore: int = 0


class ApiKeyIn(_Body):
    # Which service will hold the key.
    name: str = Field(min_length=1, max_length=64)
    scopes: list[Scope] = Field(min_length=1)
    # Requests a minute; the API's default when null.
    rate_limit_per_minute: int | None = Field(default=None, gt=0, le=100_000)
    # When it stops working; null for never (rotate or revoke it instead).
    expires_at: AwareDatetime | None = None


class RotationIn(_Body):
    # How long the old key keeps working alongside the new one; the API's default when null.
    # Zero ends it at once.
    overlap_minutes: int | None = Field(default=None, ge=0, le=30 * 24 * 60)


class ApiKeyOut(_Body):
    key_id: str
    name: str
    scopes: list[str]
    rate_limit_per_minute: int
    created_at: datetime
    created_by: str
    expires_at: datetime | None
    revoked_at: datetime | None
    # The key this one was rotated from.
    replaces: str | None
    # Whether the key is accepted now.
    active: bool

    @classmethod
    def of(cls, record: ApiKeyRecord, now: datetime) -> "ApiKeyOut":
        return cls(
            key_id=record.key_id,
            name=record.name,
            scopes=sorted(record.scopes),
            rate_limit_per_minute=record.rate_limit_per_minute,
            created_at=record.created_at,
            created_by=record.created_by,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
            replaces=record.replaces,
            active=record.active(now),
        )


class IssuedApiKeyOut(_Body):
    # The whole key, to send in the X-API-Key header. Shown this once: only its hash is stored.
    key: str
    api_key: ApiKeyOut


class AuditEntryOut(_Body):
    audit_id: int
    # When it happened: the operational wall clock at the request.
    occurred_at: datetime
    actor_type: Literal["person", "service", "operator"]
    # Keycloak's subject ID, the API key ID, or the command.
    actor_id: str
    actor_name: str
    actor_roles: list[str]
    # A stable dotted code, e.g. transaction.view or api_key.issue.
    action: str
    object_type: str | None
    object_id: str | None
    # success; denied (a 403); failed (permitted, but no such object or a conflict).
    outcome: Literal["success", "denied", "failed"]
    detail: dict[str, Any]

    @classmethod
    def of(cls, record: AuditRecord) -> "AuditEntryOut":
        return cls.model_validate({**record.__dict__, "actor_roles": list(record.actor_roles)})


class AuditPage(_Body):
    entries: list[AuditEntryOut]
    # Pass as `before` for the next (older) page; null on the last page.
    next_before: int | None


CaseStatusName = Literal["open", "investigating", "closed_fraud", "closed_legitimate"]


class CaseOut(_Body):
    case_id: int
    subject_wallet_id: str
    status: CaseStatusName
    # The event time of the Case's first Alert's Transaction.
    opened_at: datetime
    # The Keycloak subject ID and username of the Analyst it is assigned to.
    assignee_id: str | None
    assignee_name: str | None
    # When it was closed (event time), and by whom; null while it is open or investigating.
    closed_at: datetime | None
    closed_by_name: str | None
    alert_count: int
    # The highest Risk score among its Alerts.
    top_risk_score: float | None

    @classmethod
    def of(cls, case: CaseSummary) -> "CaseOut":
        return cls.model_validate(case.__dict__)


class CasePage(_Body):
    cases: list[CaseOut]
    # Pass as `before` for the next page; null on the last.
    next_before: int | None


class CaseAlertOut(_Body):
    alert_id: int
    transaction_id: str
    raised_at: datetime
    assessment: AssessmentOut
    # Taken from how the Case closed: confirmed_fraud or dismissed; null while it is still open.
    disposition: Literal["confirmed_fraud", "dismissed"] | None


class CaseEventOut(_Body):
    event_id: int
    kind: Literal["assigned", "note", "transition"]
    # The simulation clock when it happened.
    occurred_at: datetime
    actor_id: str
    actor_name: str
    # A note's text, or a transition's comment.
    body: str | None
    from_status: CaseStatusName | None
    to_status: CaseStatusName | None
    # For an assignment, the Analyst it assigned the Case to.
    assignee_id: str | None


class ReportOut(_Body):
    report_id: int
    case_id: int
    # A draft is rewritten until a Supervisor approves it; an approved report is final and the
    # only one that can be exported.
    status: Literal["draft", "approved"]
    summary: str
    # Event times of the latest draft and of the approval, and who did each.
    drafted_at: datetime
    drafted_by_name: str
    approved_at: datetime | None
    approved_by_name: str | None

    @classmethod
    def of(cls, report: Report) -> "ReportOut":
        return cls.model_validate(report.__dict__)


class CaseDetail(_Body):
    case: CaseOut
    alerts: list[CaseAlertOut]
    # The Subject wallet's latest Transactions as Subject wallet, newest first, scored or not.
    recent_transactions: list[TransactionView]
    events: list[CaseEventOut]
    # The Suspicious transaction report, once one is drafted; null before, and for any Case that
    # is not closed as fraud.
    report: ReportOut | None


class AssignIn(_Body):
    # The Analyst's Keycloak subject ID, as it appears in their token's `sub`.
    assignee_id: str = _ID
    # Their username, for display.
    assignee_name: str | None = Field(default=None, min_length=1, max_length=128)


class NoteIn(_Body):
    text: str = Field(min_length=1, max_length=4000)


class TransitionIn(_Body):
    # open -> investigating -> closed_fraud | closed_legitimate. A Case is never reopened.
    to: Literal["investigating", "closed_fraud", "closed_legitimate"]
    comment: str | None = Field(default=None, min_length=1, max_length=4000)


class WatchlistItem(_Body):
    transaction: TransactionOut
    assessment: AssessmentOut


class WatchlistPage(_Body):
    items: list[WatchlistItem]
    # Pass as `before` for the next page; null on the last.
    next_before: str | None


class ReportDraftIn(_Body):
    # What the Analyst concludes, in their own words. Phone numbers in it are masked on export.
    summary: str = Field(min_length=1, max_length=8000)


class ExportedCase(_Body):
    case_id: int
    subject_wallet_id: str
    opened_at: datetime
    closed_at: datetime
    closed_by_name: str


class ExportedTransaction(_Body):
    transaction: TransactionOut
    # The Risk assessment with its Reasons (code, message and value).
    assessment: AssessmentOut


class ExportedNote(_Body):
    kind: Literal["note", "transition"]
    occurred_at: datetime
    author: str
    text: str


class ReportExport(_Body):
    """The Suspicious transaction report as a file, for the institution to file through its own
    channel. Wallets appear only by pseudonymised ID, and phone numbers in free text are masked."""

    format_version: Literal[1] = 1
    report_id: int
    case: ExportedCase
    summary: str
    drafted_by: str
    drafted_at: datetime
    approved_by: str
    approved_at: datetime
    # The Transactions Alerted on the Case, oldest first.
    transactions: list[ExportedTransaction]
    # The Case's notes, and the comments on its moves, oldest first.
    notes: list[ExportedNote]

    @classmethod
    def of(cls, file: ReportFile) -> "ReportExport":
        report, case = file.report, file.case
        assert case.closed_at is not None and case.closed_by_name is not None
        assert report.approved_at is not None and report.approved_by_name is not None
        return cls(
            report_id=report.report_id,
            case=ExportedCase(
                case_id=case.case_id,
                subject_wallet_id=case.subject_wallet_id,
                opened_at=case.opened_at,
                closed_at=case.closed_at,
                closed_by_name=case.closed_by_name,
            ),
            summary=mask_phone_numbers(report.summary),
            drafted_by=report.drafted_by_name,
            drafted_at=report.drafted_at,
            approved_by=report.approved_by_name,
            approved_at=report.approved_at,
            transactions=[
                ExportedTransaction(
                    transaction=TransactionOut.of(t), assessment=AssessmentOut.of(a)
                )
                for t, a in file.transactions
            ],
            notes=[
                ExportedNote(
                    kind="note" if e.kind == "note" else "transition",
                    occurred_at=e.occurred_at,
                    author=e.actor_name,
                    text=mask_phone_numbers(e.body or ""),
                )
                for e in file.notes
            ],
        )
