"""The event contracts: one Pydantic model per topic, each the source of its versioned JSON Schema.

The JSON Schema files in `schemas/` are the documented ledger contract (ADR 0004). `txrisk schemas`
writes them from these models, and a test checks the two agree, so an event passes the producer
and the consumer exactly when it is valid against its published schema.

The models are strict: a number is never read from a string, nor a time from a number, so what
passes here is what the schema allows. Money is a JSON number of GHS with at most two decimals,
as in the sync API.
"""

from datetime import UTC
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from txrisk.features import Transaction
from txrisk.scoring import Reason, RiskAssessment, RiskBand
from txrisk.store.tables import COUNTERPARTY_COLUMNS

SCHEMA_VERSION = 1
# Where the published schemas say they live: `$id` is `<base><topic>.v<version>.json`.
SCHEMA_BASE_URI = "https://txrisk.example/schemas/"

TransactionType = Literal["cash_in", "cash_out", "transfer", "payment", "bill_payment"]

# An RFC 3339 time with an explicit offset; a time without one is ambiguous, so it is refused.
_TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
_ID = Field(min_length=1, max_length=128)
_OPTIONAL_ID = Field(default=None, min_length=1, max_length=128)
# numeric(14, 2), as Postgres stores money: an amount published is exactly the amount scored.
_MONEY_LIMIT = 1e12


def _event_time(description: str) -> Any:
    return Field(description=description, json_schema_extra={"pattern": _TIME_PATTERN})


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _counterparty_rules() -> list[dict[str, Any]]:
    """The JSON Schema form of "exactly one Counterparty, of the kind the Transaction type deals
    with, and money moves in for a Cash-in only"; the model checks the same in `_consistent`."""
    columns = sorted(set(COUNTERPARTY_COLUMNS.values()))
    rules = []
    for transaction_type, column in COUNTERPARTY_COLUMNS.items():
        others = {c: {"type": "null"} for c in columns if c != column}
        rules.append(
            {
                "if": {
                    "properties": {"transaction_type": {"const": transaction_type}},
                    "required": ["transaction_type"],
                },
                "then": {
                    "required": [column],
                    "properties": {
                        column: {"type": "string"},
                        "direction": {"const": "in" if transaction_type == "cash_in" else "out"},
                        **others,
                    },
                },
            }
        )
    return rules


class TransactionCreated(Event):
    """A Transaction as the ledger reports it, published to `transaction.created` when it happens.

    It names exactly one Counterparty, in the field for its Transaction type: `agent_id` for a
    Cash-in or Cash-out, `counterparty_wallet_id` for a Transfer, `merchant_id` for a Payment and
    `biller_id` for a Bill payment. There is no balance after.
    """

    model_config = ConfigDict(
        title="transaction.created",
        json_schema_extra={"allOf": _counterparty_rules()},  # type: ignore[dict-item]
    )

    transaction_id: str = _ID
    occurred_at: AwareDatetime = _event_time(
        "When it happened, with a UTC offset; it is the scoring clock (ADR 0005)."
    )
    transaction_type: TransactionType
    amount: float = Field(
        gt=0, lt=_MONEY_LIMIT, multiple_of=0.01, description="GHS, at most two decimals."
    )
    subject_wallet_id: str = Field(
        min_length=1,
        max_length=128,
        description="The receiving Wallet for a Cash-in, the sending Wallet otherwise.",
    )
    direction: Literal["in", "out"] = Field(
        description="Into the Subject wallet for a Cash-in, out of it for every other type."
    )
    subject_balance_before: float = Field(
        ge=0,
        lt=_MONEY_LIMIT,
        multiple_of=0.01,
        description="The Subject wallet's balance before the Transaction, in GHS.",
    )
    channel: Literal["ussd", "app"]
    device_id: str = _ID
    region: str = Field(min_length=1, max_length=64)
    counterparty_wallet_id: str | None = _OPTIONAL_ID
    agent_id: str | None = _OPTIONAL_ID
    merchant_id: str | None = _OPTIONAL_ID
    biller_id: str | None = _OPTIONAL_ID

    @model_validator(mode="after")
    def _consistent(self) -> Self:
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

    @classmethod
    def of(cls, transaction: Transaction) -> "TransactionCreated":
        """The event for a Transaction; raises `pydantic.ValidationError` if it breaks the
        contract, so nothing invalid is ever published."""
        return cls.model_validate(
            {
                "transaction_id": transaction.transaction_id,
                "occurred_at": transaction.occurred_at,
                "transaction_type": transaction.transaction_type,
                "amount": transaction.amount,
                "subject_wallet_id": transaction.subject_wallet_id,
                "direction": transaction.direction,
                "subject_balance_before": transaction.subject_balance_before,
                "channel": transaction.channel,
                "device_id": transaction.device_id,
                "region": transaction.region,
                COUNTERPARTY_COLUMNS[transaction.transaction_type]: transaction.counterparty_id,
            }
        )

    def to_transaction(self) -> Transaction:
        return Transaction(
            transaction_id=self.transaction_id,
            occurred_at=self.occurred_at.astimezone(UTC),
            transaction_type=self.transaction_type,
            amount=self.amount,
            subject_wallet_id=self.subject_wallet_id,
            direction=self.direction,
            counterparty_id=getattr(self, COUNTERPARTY_COLUMNS[self.transaction_type]),
            region=self.region,
            subject_balance_before=self.subject_balance_before,
            device_id=self.device_id,
            channel=self.channel,
        )


class SimSwapRecorded(Event):
    """A SIM swap on a Wallet's phone number, published to `sim_swap.recorded` by the operator
    when it happens. Scoring sees it only for Transactions after `occurred_at`."""

    model_config = ConfigDict(title="sim_swap.recorded")

    wallet_id: str = _ID
    occurred_at: AwareDatetime = _event_time("When the SIM swap happened, with a UTC offset.")


class ScoredReason(Event):
    model_config = ConfigDict(title="Reason")

    code: str = Field(min_length=1, description="Stable, for filtering and counting.")
    message: str
    value: float | str | None = Field(
        description="The actual value involved: a number, a word, or null where none applies."
    )


class ComponentScores(Event):
    """Each component of the Risk score on a 0-1 scale; null without a model for it."""

    model_config = ConfigDict(title="ComponentScores")

    rules: float | None = Field(ge=0, le=1)
    anomaly: float | None = Field(ge=0, le=1)
    supervised: float | None = Field(ge=0, le=1)


class ScoreProduced(Event):
    """The Risk assessment of a Transaction, published to `score.produced` once it is stored.

    A Transaction has one stored Risk assessment, so every `score.produced` for a transaction ID
    carries the same assessment: a redelivered `transaction.created` republishes it unchanged.
    """

    model_config = ConfigDict(title="score.produced")

    transaction_id: str = _ID
    subject_wallet_id: str = _ID
    assessed_at: AwareDatetime = _event_time(
        "The simulation clock when it was scored: the Transaction's own event time (ADR 0005)."
    )
    risk_score: float = Field(ge=0, le=100)
    risk_band: Literal["Low", "Medium", "High"]
    reasons: list[ScoredReason] = Field(
        max_length=3, description="At most three, Critical rule hits first, then by contribution."
    )
    component_scores: ComponentScores
    model_version: str = Field(min_length=1, description="The model version that scored it.")
    degraded: bool = Field(
        description="True when the rules alone scored because the Champion model was unavailable."
    )

    @classmethod
    def of(cls, transaction: Transaction, assessment: RiskAssessment) -> "ScoreProduced":
        components = assessment.component_scores
        return cls.model_validate(
            {
                "transaction_id": assessment.transaction_id,
                "subject_wallet_id": transaction.subject_wallet_id,
                "assessed_at": transaction.occurred_at,
                "risk_score": assessment.risk_score,
                "risk_band": assessment.risk_band.value,
                "reasons": [
                    {"code": r.code, "message": r.message, "value": r.value}
                    for r in assessment.reasons
                ],
                "component_scores": {
                    name: components.get(name) for name in ("rules", "anomaly", "supervised")
                },
                "model_version": assessment.model_version,
                "degraded": assessment.degraded,
            }
        )

    def to_assessment(self) -> RiskAssessment:
        return RiskAssessment(
            transaction_id=self.transaction_id,
            risk_score=self.risk_score,
            risk_band=RiskBand(self.risk_band),
            # A stored Reason without a finite value carries null.
            reasons=tuple(Reason(r.code, r.message, r.value) for r in self.reasons),  # type: ignore[arg-type]
            component_scores=self.component_scores.model_dump(),
            model_version=self.model_version,
            degraded=self.degraded,
        )


class FraudDiscovered(Event):
    """A Fraud discovery: the moment a past Transaction became known to be fraudulent, through a
    customer complaint, a chargeback or a Case closed as fraud. It is the only source of Fraud
    labels; Ground truth is never published (ADR 0007)."""

    model_config = ConfigDict(title="fraud.discovered")

    transaction_id: str = _ID
    discovered_at: AwareDatetime = _event_time("When the fraud became known, with a UTC offset.")
    source: Literal["complaint", "chargeback", "case"]


# Every topic's event, by topic name.
EVENTS: dict[str, type[Event]] = {
    "transaction.created": TransactionCreated,
    "sim_swap.recorded": SimSwapRecorded,
    "score.produced": ScoreProduced,
    "fraud.discovered": FraudDiscovered,
}


class InvalidEvent(ValueError):
    """A payload that does not match its topic's contract."""


def schema_file(topic: str) -> str:
    return f"{topic}.v{SCHEMA_VERSION}.json"


def json_schema(topic: str) -> dict[str, Any]:
    """The published JSON Schema (draft 2020-12) of the topic's event."""
    schema = EVENTS[topic].model_json_schema(mode="validation")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_BASE_URI + schema_file(topic),
        **schema,
    }


def decode[E: Event](model: type[E], payload: bytes) -> E:
    """The event in `payload`; raises `InvalidEvent` if it breaks the contract."""
    try:
        return model.model_validate_json(payload)
    except ValidationError as e:
        raise InvalidEvent(str(e)) from e


def encode(event: Event) -> bytes:
    """The event as published, in JSON; a Transaction leaves out the Counterparty fields it
    does not use."""
    return event.model_dump_json(exclude_none=isinstance(event, TransactionCreated)).encode()
