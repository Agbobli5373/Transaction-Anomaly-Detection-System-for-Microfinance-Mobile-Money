"""`txrisk seed-cases`: the demo Cases the workbench's end-to-end tests work on, made through the
store the way scoring makes them. Against real Postgres."""

import json
from typing import Any

import pytest

from txrisk.cli import main
from txrisk.store import cases, create_engine, watchlist


def seed(capsys: pytest.CaptureFixture[str], database: str, *args: str) -> list[dict[str, Any]]:
    assert main(["seed-cases", "--database-url", database, *args]) == 0
    made: list[dict[str, Any]] = json.loads(capsys.readouterr().out)["cases"]
    return made


def test_seed_cases_makes_cases_with_alerts_reasons_and_recent_transactions(
    database: str, capsys: pytest.CaptureFixture[str]
):
    made = seed(capsys, database, "--tag", "t1", "--count", "2")

    assert [c["wallet_id"] for c in made] == ["w-t1-000", "w-t1-001"]
    engine = create_engine(database)
    with engine.connect() as connection:
        case = cases.get_case(connection, made[0]["case_id"])
        assert case is not None
        assert (case.status, case.alert_count, case.assignee_id) == ("open", 1, None)
        (alert,) = cases.case_alerts(connection, case.case_id)
        assert alert.assessment.risk_band == "High"
        assert alert.assessment.reasons[0].code == "SIM_SWAP_DRAIN"
        recent = cases.recent_transactions(connection, "w-t1-000", limit=20)
        assert len(recent) == 5
        assert recent[0][0].transaction_id == alert.transaction_id


def test_seed_cases_can_assign_every_case_to_an_analyst(
    database: str, capsys: pytest.CaptureFixture[str]
):
    made = seed(
        capsys,
        database,
        "--tag",
        "t2",
        "--count",
        "1",
        "--assignee-id",
        "sub-ama",
        "--assignee-name",
        "Ama",
    )

    with create_engine(database).connect() as connection:
        case = cases.get_case(connection, made[0]["case_id"])
        assert case is not None
        assert (case.assignee_id, case.assignee_name) == ("sub-ama", "Ama")


def test_seed_cases_again_with_the_same_tag_changes_nothing(
    database: str, capsys: pytest.CaptureFixture[str]
):
    first = seed(capsys, database, "--tag", "t3", "--count", "2", "--medium", "4")
    again = seed(
        capsys,
        database,
        *("--tag", "t3", "--count", "2", "--medium", "4"),
        *("--assignee-id", "sub-ama", "--assignee-name", "Ama"),
    )

    assert again == first
    with create_engine(database).connect() as connection:
        found = cases.list_cases(connection, limit=50)
        assert len(found) == 2
        assert all(c.alert_count == 1 for c in found)


def test_seed_cases_makes_medium_transactions_across_regions_and_types(
    database: str, capsys: pytest.CaptureFixture[str]
):
    seed(capsys, database, "--tag", "t4", "--count", "1", "--medium", "6")

    with create_engine(database).connect() as connection:
        listed = watchlist.list_medium(connection, limit=50)
        assert len(listed) == 6
        assert len({w.transaction.region for w in listed}) > 1
        assert len({w.transaction.transaction_type for w in listed}) > 1
        assert all(w.assessment.risk_band == "Medium" for w in listed)


def test_seed_cases_needs_the_assignee_id_and_name_together(
    database: str, capsys: pytest.CaptureFixture[str]
):
    code = main(["seed-cases", "--database-url", database, "--assignee-id", "sub-ama"])

    assert code == 2
    assert "together" in capsys.readouterr().err
