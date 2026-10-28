"""The contract checks CI runs: the OpenAPI snapshot and breaking event schema changes."""

import json
from pathlib import Path
from typing import Any

import pytest

from txrisk.contract_check import breaking_changes, main, openapi_spec, schema_changes

ROOT = Path(__file__).resolve().parents[1]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "transaction_id": {"type": "string"},
        "amount": {"type": "number"},
        "channel": {"enum": ["USSD", "App"]},
        "reasons": {"type": "array", "items": {"type": "object", "properties": {"code": {}}}},
    },
    "required": ["transaction_id"],
}


def _with(**changes: Any) -> dict[str, Any]:
    return {**json.loads(json.dumps(SCHEMA)), **changes}


def test_the_committed_openapi_snapshot_is_the_spec_the_api_serves():
    snapshot = json.loads((ROOT / "contracts" / "openapi.json").read_text(encoding="utf-8"))

    assert snapshot == openapi_spec(), (
        "contracts/openapi.json is stale; regenerate it with "
        "`uv run python -m txrisk.contract_check openapi > contracts/openapi.json`"
    )


def test_an_unchanged_schema_has_no_breaking_changes():
    assert breaking_changes(SCHEMA, _with()) == []


def test_adding_an_optional_property_or_widening_an_enum_is_not_breaking():
    widened = _with(
        properties={
            **SCHEMA["properties"],
            "note": {"type": "string"},
            "channel": {"enum": ["USSD", "App", "Web"]},
        }
    )

    assert breaking_changes(SCHEMA, widened) == []


def test_removing_a_property_is_breaking_even_when_nested():
    nested = json.loads(json.dumps(SCHEMA))
    del nested["properties"]["reasons"]["items"]["properties"]["code"]
    del nested["properties"]["amount"]

    assert breaking_changes(SCHEMA, nested) == [
        "$.amount: property removed",
        "$.reasons[].code: property removed",
    ]


def test_a_shared_definition_is_checked_too():
    base = {"$defs": {"Reason": {"properties": {"code": {"type": "string"}, "value": {}}}}}
    current = {"$defs": {"Reason": {"properties": {"code": {"type": "integer"}}}}}

    assert breaking_changes(base, current) == [
        "$.$defs.Reason.code: type changed from ['string'] to ['integer']",
        "$.$defs.Reason.value: property removed",
    ]
    assert breaking_changes(base, {"$defs": {}}) == ["$.$defs.Reason: definition removed"]


def test_changing_a_type_or_narrowing_an_enum_is_breaking():
    changed = _with(
        properties={
            **SCHEMA["properties"],
            "amount": {"type": "string"},
            "channel": {"enum": ["USSD"]},
        }
    )

    assert breaking_changes(SCHEMA, changed) == [
        "$.amount: type changed from ['number'] to ['string']",
        "$.channel: enum values removed: ['App']",
    ]


def test_requiring_a_new_property_is_breaking():
    assert breaking_changes(SCHEMA, _with(required=["transaction_id", "amount"])) == [
        "$.amount: newly required"
    ]


def _write(folder: Path, name: str, schema: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(schema), encoding="utf-8")


def test_schema_folders_are_compared_file_by_file(tmp_path: Path):
    base, current = tmp_path / "base", tmp_path / "current"
    _write(base, "transaction.created.json", SCHEMA)
    _write(base, "score.produced.json", SCHEMA)
    _write(current, "transaction.created.json", _with(required=["transaction_id", "amount"]))
    _write(current, "fraud.discovered.json", SCHEMA)

    assert schema_changes(base, current) == [
        "score.produced.json: schema removed",
        "transaction.created.json $.amount: newly required",
    ]


def test_there_is_nothing_to_compare_before_schemas_exist(tmp_path: Path):
    assert schema_changes(tmp_path / "missing", tmp_path) == []


def test_the_schemas_command_fails_on_a_breaking_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    base, current = tmp_path / "base", tmp_path / "current"
    _write(base, "transaction.created.json", SCHEMA)
    _write(current, "transaction.created.json", _with(required=["transaction_id", "amount"]))

    assert main(["schemas", str(base), str(current)]) == 1
    assert "breaking: transaction.created.json $.amount: newly required" in capsys.readouterr().err
    assert main(["schemas", str(base), str(base)]) == 0


def test_the_openapi_command_prints_the_served_spec(capsys: pytest.CaptureFixture[str]):
    assert main(["openapi"]) == 0
    assert json.loads(capsys.readouterr().out) == openapi_spec()
