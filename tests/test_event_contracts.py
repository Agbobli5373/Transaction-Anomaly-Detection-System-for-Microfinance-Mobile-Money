"""The event contracts: the published JSON Schemas, and the models that validate events at both
ends, accept and refuse exactly the same events."""

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError, validators
from scoring_helpers import NOW, tx

from txrisk.cli import main
from txrisk.events import (
    EVENTS,
    InvalidEvent,
    ScoreProduced,
    TransactionCreated,
    decode,
    encode,
    json_schema,
    schema_file,
)
from txrisk.scoring import Reason, RiskAssessment, RiskBand

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"


def _multiple_of(validator: Any, divisor: Any, instance: Any, schema: Any) -> Any:
    """`multipleOf` in decimal, as JSON means it: 80.28 is a multiple of 0.01, though not in
    binary floating point, where the stock check works."""
    if not validator.is_type(instance, "number"):
        return
    if Decimal(str(instance)) % Decimal(str(divisor)) != 0:
        yield ValidationError(f"{instance} is not a multiple of {divisor}")


_Validator = validators.extend(Draft202012Validator, {"multipleOf": _multiple_of})


def published(topic: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / schema_file(topic)).read_text(encoding="utf-8"))


def schema_accepts(topic: str, event: dict[str, Any]) -> bool:
    validator = _Validator(published(topic), format_checker=Draft202012Validator.FORMAT_CHECKER)
    return validator.is_valid(event)


def model_accepts(topic: str, event: dict[str, Any]) -> bool:
    try:
        decode(EVENTS[topic], json.dumps(event).encode())
    except InvalidEvent:
        return False
    return True


TRANSFER: dict[str, Any] = {
    "transaction_id": "T000000001",
    "occurred_at": "2030-06-01T14:00:00Z",
    "transaction_type": "transfer",
    "amount": 80.28,
    "subject_wallet_id": "w-subject",
    "direction": "out",
    "subject_balance_before": 1000.0,
    "channel": "app",
    "device_id": "D-own-phone",
    "region": "Ashanti",
    "counterparty_wallet_id": "w-friend",
}
CASH_IN: dict[str, Any] = {
    **{k: v for k, v in TRANSFER.items() if k != "counterparty_wallet_id"},
    "transaction_type": "cash_in",
    "direction": "in",
    "agent_id": "A00001",
}
SCORE: dict[str, Any] = {
    "transaction_id": "T000000001",
    "subject_wallet_id": "w-subject",
    "assessed_at": "2030-06-01T14:00:00Z",
    "risk_score": 85.0,
    "risk_band": "High",
    "reasons": [
        {"code": "SIM_SWAP_DRAIN", "message": "90% of the balance moved", "value": 0.9},
        {"code": "NEW_DEVICE_BURST", "message": "a new device", "value": "D-new"},
        {"code": "AMOUNT_VS_MEDIAN", "message": "unusual amount", "value": None},
    ],
    "component_scores": {"rules": 0.95, "anomaly": None, "supervised": None},
    "model_version": "rules-only",
    "degraded": False,
}
SIM_SWAP: dict[str, Any] = {"wallet_id": "w-subject", "occurred_at": "2030-06-01T12:00:00+00:00"}
DISCOVERY: dict[str, Any] = {
    "transaction_id": "T000000001",
    "discovered_at": "2030-06-09T10:30:00.123456Z",
    "source": "complaint",
}

VALID = [
    ("transaction.created", TRANSFER),
    ("transaction.created", CASH_IN),
    ("transaction.created", {**TRANSFER, "occurred_at": "2030-06-01T16:00:00.5+02:00"}),
    ("transaction.created", {**TRANSFER, "amount": 5, "subject_balance_before": 0}),
    ("transaction.created", {**TRANSFER, "agent_id": None}),  # an unused Counterparty as null
    ("sim_swap.recorded", SIM_SWAP),
    ("score.produced", SCORE),
    ("score.produced", {**SCORE, "reasons": [], "risk_band": "Low", "risk_score": 0}),
    ("fraud.discovered", DISCOVERY),
    ("fraud.discovered", {**DISCOVERY, "source": "case"}),
]


def _without(event: dict[str, Any], field: str) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k != field}


INVALID = [
    ("transaction.created", _without(TRANSFER, "amount")),
    ("transaction.created", {**TRANSFER, "balance_after": 919.72}),  # no balance after
    ("transaction.created", {**TRANSFER, "amount": "80.28"}),  # money is a number
    ("transaction.created", {**TRANSFER, "amount": 10.001}),  # more decimals than money has
    ("transaction.created", {**TRANSFER, "amount": 0}),
    ("transaction.created", {**TRANSFER, "subject_balance_before": -1}),
    ("transaction.created", {**TRANSFER, "occurred_at": "2030-06-01T14:00:00"}),  # no offset
    ("transaction.created", {**TRANSFER, "occurred_at": 1906552800}),  # not a time
    ("transaction.created", {**TRANSFER, "transaction_type": "deposit"}),
    ("transaction.created", {**TRANSFER, "channel": "web"}),
    ("transaction.created", {**TRANSFER, "transaction_id": ""}),
    ("transaction.created", {**TRANSFER, "agent_id": "A00001"}),  # two Counterparties
    ("transaction.created", _without(TRANSFER, "counterparty_wallet_id")),  # none
    ("transaction.created", {**CASH_IN, "merchant_id": "M00001"}),
    ("transaction.created", {**TRANSFER, "direction": "in"}),  # in is for a Cash-in only
    ("transaction.created", {**CASH_IN, "direction": "out"}),
    ("transaction.created", {**CASH_IN, "transaction_type": "payment"}),  # a Payment has a Merchant
    ("sim_swap.recorded", _without(SIM_SWAP, "occurred_at")),
    ("sim_swap.recorded", {**SIM_SWAP, "occurred_at": "2030-06-01"}),
    ("score.produced", {**SCORE, "risk_band": "Critical"}),
    ("score.produced", {**SCORE, "risk_score": 100.5}),
    ("score.produced", {**SCORE, "reasons": SCORE["reasons"] * 2}),  # at most three
    (
        "score.produced",
        {**SCORE, "component_scores": {"rules": 1.5, "anomaly": 0, "supervised": 0}},
    ),
    ("score.produced", {**SCORE, "component_scores": {"rules": 0.5}}),
    ("score.produced", {**SCORE, "degraded": "no"}),
    ("score.produced", _without(SCORE, "model_version")),
    ("fraud.discovered", {**DISCOVERY, "source": "ground_truth"}),
    ("fraud.discovered", {**DISCOVERY, "is_fraud": True}),
]


@pytest.mark.parametrize("topic", list(EVENTS))
def test_the_published_schemas_are_the_ones_the_models_validate_with(topic: str):
    """`schemas/` is written by `txrisk schemas`; a change to a model must be republished."""
    assert published(topic) == json_schema(topic)
    assert published(topic)["$id"].endswith(f"/{topic}.v1.json")


def test_txrisk_schemas_writes_every_topics_schema(tmp_path: Path):
    assert main(["schemas", "--out", str(tmp_path)]) == 0

    written = {p.name: json.loads(p.read_text()) for p in tmp_path.iterdir()}
    assert written == {schema_file(topic): json_schema(topic) for topic in EVENTS}


@pytest.mark.parametrize(("topic", "event"), VALID)
def test_valid_events_pass_both_the_schema_and_the_model(topic: str, event: dict[str, Any]):
    assert schema_accepts(topic, event)
    assert model_accepts(topic, event)


@pytest.mark.parametrize(("topic", "event"), INVALID)
def test_invalid_events_fail_both_the_schema_and_the_model(topic: str, event: dict[str, Any]):
    assert not schema_accepts(topic, event)
    assert not model_accepts(topic, event)


@pytest.mark.parametrize("payload", [b"", b"not json", b"[]", b"null"])
def test_a_payload_that_is_not_an_event_is_invalid(payload: bytes):
    with pytest.raises(InvalidEvent):
        decode(TransactionCreated, payload)


@pytest.mark.parametrize(
    "transaction_type", ["cash_in", "cash_out", "transfer", "payment", "bill_payment"]
)
def test_a_transaction_survives_publishing_unchanged(transaction_type: str):
    counterparty = {"cash_in": "A1", "cash_out": "A1", "payment": "M1", "bill_payment": "B1"}
    transaction = tx(
        NOW,
        transaction_type,
        counterparty=counterparty.get(transaction_type, "w-friend"),
        amount=80.28,
        device="D-own-phone",
    )
    transaction = dataclasses.replace(transaction, channel="ussd")

    payload = encode(TransactionCreated.of(transaction))

    assert decode(TransactionCreated, payload).to_transaction() == transaction
    assert schema_accepts("transaction.created", json.loads(payload))


def test_a_transaction_that_breaks_the_contract_cannot_be_published():
    without_channel = tx(NOW)  # the hand-built Transactions have no Channel

    with pytest.raises(ValueError, match="channel"):
        TransactionCreated.of(without_channel)


def test_an_assessment_survives_publishing_unchanged():
    transaction = tx(NOW)
    assessment = RiskAssessment(
        transaction_id=transaction.transaction_id,
        risk_score=73.25,
        risk_band=RiskBand.HIGH,
        reasons=(Reason("SIM_SWAP_DRAIN", "90% of the balance moved", 0.9),),
        component_scores={"rules": 0.95, "anomaly": 0.4, "supervised": 0.012345678901},
        model_version="20300601-abc",
    )

    payload = encode(ScoreProduced.of(transaction, assessment))

    assert decode(ScoreProduced, payload).to_assessment() == assessment
    assert schema_accepts("score.produced", json.loads(payload))
    assert json.loads(payload)["assessed_at"] == "2030-06-01T14:00:00Z"
