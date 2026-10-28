"""Cases: Alerts joining them, the Case API and its permissions, closing as fraud and the outbox
that publishes the Fraud discoveries, and the audit of every action. Against real Postgres, and
Redpanda for the publishing."""

import dataclasses
import json
import re
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from api_helpers import WallClock, body, issue_key, settings
from api_helpers import signed_in as bearer
from api_helpers import with_key as service
from fastapi.testclient import TestClient
from httpx import Response
from idp_helpers import LocalIdentityProvider
from scoring_helpers import DAY, HOUR, NOW, REGISTERED, SUBJECT, routine, tx
from test_stream import Stream

from txrisk.api import create_app
from txrisk.events import (
    DeliveryFailed,
    FraudDiscovered,
    OutboxPublisher,
    Publisher,
    Topics,
    decode,
    ensure_topics,
)
from txrisk.features import Transaction
from txrisk.scoring import Reason, RiskAssessment, RiskBand
from txrisk.store import FraudLabels, SimulationClock, cases, create_engine, records
from txrisk.store.roles import GRANTS

FRIEND = "w-friend"
OTHER = "w-other"
STRANGER = "w-stranger"
AGENT = "A00001"
# A phone number as it would appear if one leaked: nine to fifteen digits standing alone.
PHONE_SHAPE = re.compile(r"(?<![\w.-])\+?\d{9,15}(?![\w.-])")

SUBJECTS = {
    "ama": "sub-ama",
    "yaw": "sub-yaw",
    "esi": "sub-esi",
}


def assessment_for(transaction: Transaction, band: RiskBand, score: float) -> RiskAssessment:
    return RiskAssessment(
        transaction_id=transaction.transaction_id,
        risk_score=score,
        risk_band=band,
        reasons=(Reason("SIM_SWAP_DRAIN", "Balance drained after a SIM swap", 0.9),),
        component_scores={"rules": 0.9, "anomaly": None, "supervised": None},
    )


class Desk:
    """The API called by two Analysts (ama, yaw), a Supervisor (esi), an Admin and a service,
    over a market of a few Wallets, with a hand-moved wall clock."""

    def __init__(self, database: str, client: TestClient, idp: LocalIdentityProvider) -> None:
        self.database = database
        self.client = client
        self.engine = create_engine(database)
        self.ama = bearer(idp.access("analyst", preferred_username="ama", sub=SUBJECTS["ama"]))
        self.yaw = bearer(idp.access("analyst", preferred_username="yaw", sub=SUBJECTS["yaw"]))
        self.esi = bearer(idp.access("supervisor", preferred_username="esi", sub=SUBJECTS["esi"]))
        self.admin = bearer(idp.access("admin", preferred_username="kofi"))
        self.service = service(issue_key(database))
        self.clock = SimulationClock(self.engine)

    def alert(
        self, at: datetime = NOW, *, wallet: str = SUBJECT, score: float = 90.0
    ) -> Transaction:
        """Store a High Transaction the way scoring does: its assessment and its Alert, in one
        transaction."""
        transaction = dataclasses.replace(
            tx(at, wallet=wallet, amount=900.0, counterparty=STRANGER), channel="app"
        )
        with self.engine.begin() as connection:
            records.record_transaction(connection, transaction)
            records.save_assessment(
                connection, assessment_for(transaction, RiskBand.HIGH, score), assessed_at=at
            )
            records.raise_alert(connection, transaction)
        return transaction

    def medium(self, at: datetime = NOW, **fields: Any) -> Transaction:
        transaction = dataclasses.replace(tx(at, **fields), channel="app")
        with self.engine.begin() as connection:
            records.record_transaction(connection, transaction)
            records.save_assessment(
                connection, assessment_for(transaction, RiskBand.MEDIUM, 45.0), assessed_at=at
            )
        return transaction

    def case_ids(self, wallet: str = SUBJECT) -> list[int]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.text("SELECT case_id FROM cases WHERE subject_wallet_id = :w ORDER BY 1"),
                    {"w": wallet},
                ).scalars()
            )

    def case_of(self, transaction: Transaction) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    sa.text("SELECT case_id FROM alerts WHERE transaction_id = :t"),
                    {"t": transaction.transaction_id},
                ).scalar_one()
            )

    def get(self, path: str, who: dict[str, str], **params: Any) -> Response:
        return self.client.get(path, params=params, headers=who)

    def post(self, path: str, who: dict[str, str], json: dict[str, Any]) -> Response:
        return self.client.post(path, json=json, headers=who)

    def assign(self, case_id: int, to: str = "ama", who: dict[str, str] | None = None) -> Response:
        return self.post(
            f"/v1/cases/{case_id}/assign",
            who or self.esi,
            {"assignee_id": SUBJECTS[to], "assignee_name": to},
        )

    def move(self, case_id: int, to: str, who: dict[str, str] | None = None) -> Response:
        return self.post(f"/v1/cases/{case_id}/transition", who or self.ama, {"to": to})

    def audit(self, **where: str) -> list[dict[str, Any]]:
        clause = " AND ".join(f"{k} = :{k}" for k in where) or "true"
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text(f"SELECT * FROM audit_log WHERE {clause} ORDER BY audit_id"), where
            )
            return [dict(r._mapping) for r in rows]

    def labels(self) -> dict[str, tuple[datetime, str]]:
        with self.engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT * FROM fraud_labels"))
            return {r.transaction_id: (r.discovered_at, r.source) for r in rows}

    def outbox(self) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.text("SELECT * FROM outbox ORDER BY outbox_id")))


@pytest.fixture
def desk(database: str, local_idp: LocalIdentityProvider) -> Iterator[Desk]:
    engine = create_engine(database)
    with engine.begin() as connection:
        for wallet in (SUBJECT, OTHER, FRIEND, STRANGER):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )
        records.add_agent(connection, AGENT, region="Ashanti")
    engine.dispose()
    app = create_app(
        settings(database, oidc_issuer=local_idp.issuer, oidc_jwks_url=local_idp.jwks_url),
        wall_clock=WallClock(),
    )
    with TestClient(app) as client:
        yield Desk(database, client, local_idp)


# --- Alerts join Cases ---------------------------------------------------------------------


def test_a_first_alert_opens_a_case_and_later_alerts_for_the_wallet_join_it(desk: Desk):
    first = desk.alert(NOW)
    second = desk.alert(NOW + HOUR)

    assert desk.case_of(first) == desk.case_of(second)
    assert len(desk.case_ids()) == 1
    case = desk.get(f"/v1/cases/{desk.case_of(first)}", desk.esi).json()["case"]
    assert case["status"] == "open"
    assert case["alert_count"] == 2
    assert case["opened_at"] == NOW.isoformat().replace("+00:00", "Z")


def test_an_alert_for_another_wallet_opens_a_case_of_its_own(desk: Desk):
    mine, theirs = desk.alert(wallet=SUBJECT), desk.alert(wallet=OTHER)

    assert desk.case_of(mine) != desk.case_of(theirs)


def test_an_investigating_case_still_takes_new_alerts_but_a_closed_one_does_not(desk: Desk):
    first = desk.alert(NOW)
    case_id = desk.case_of(first)
    desk.assign(case_id)
    desk.move(case_id, "investigating")
    assert desk.case_of(desk.alert(NOW + HOUR)) == case_id

    assert desk.move(case_id, "closed_legitimate").status_code == 200
    later = desk.alert(NOW + 2 * HOUR)

    assert desk.case_of(later) != case_id
    assert len(desk.case_ids()) == 2


def test_concurrent_alerts_for_one_wallet_open_exactly_one_case(desk: Desk):
    barrier = threading.Barrier(8)

    def raise_one(n: int) -> Transaction:
        barrier.wait()
        return desk.alert(NOW + n * HOUR)

    with ThreadPoolExecutor(8) as pool:
        raised = list(pool.map(raise_one, range(8)))

    assert len(desk.case_ids()) == 1
    assert {desk.case_of(t) for t in raised} == set(desk.case_ids())


def test_the_database_refuses_a_second_active_case_for_a_wallet(desk: Desk):
    desk.alert()

    with pytest.raises(sa.exc.IntegrityError), desk.engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO cases (subject_wallet_id, opened_at) VALUES (:w, :at)"),
            {"w": SUBJECT, "at": NOW},
        )


def test_scoring_a_high_transaction_through_the_api_puts_its_alert_in_a_case(desk: Desk):
    with desk.engine.begin() as connection:
        for t in routine():
            records.record_transaction(connection, dataclasses.replace(t, channel="app"))
        records.add_sim_swap(connection, SUBJECT, NOW - 2 * HOUR)
    drain = dataclasses.replace(
        tx(NOW, amount=900.0, counterparty=STRANGER, balance=1000.0), channel="app"
    )

    first = desk.client.post("/v1/score", json=body(drain), headers=desk.service)
    again = desk.client.post("/v1/score", json=body(drain), headers=desk.service)

    assert first.json()["risk_band"] == again.json()["risk_band"] == "High"
    assert desk.case_of(drain) in desk.case_ids()
    assert len(desk.case_ids()) == 1  # the replay raised no second Alert, nor a second Case


# --- permissions ---------------------------------------------------------------------------


def test_only_a_supervisor_can_assign_a_case(desk: Desk):
    case_id = desk.case_of(desk.alert())

    for who in (desk.ama, desk.admin, desk.service):
        assert desk.assign(case_id, who=who).status_code == 403
    assigned = desk.assign(case_id, "ama")

    assert assigned.status_code == 200
    assert assigned.json()["case"]["assignee_id"] == SUBJECTS["ama"]
    assert assigned.json()["events"][-1]["kind"] == "assigned"


def test_an_analyst_not_assigned_to_a_case_cannot_move_or_note_it(desk: Desk):
    case_id = desk.case_of(desk.alert())

    # Unassigned: no Analyst may work it, though they can see it.
    assert desk.move(case_id, "investigating", desk.ama).status_code == 403
    assert desk.post(f"/v1/cases/{case_id}/notes", desk.ama, {"text": "x"}).status_code == 403
    desk.assign(case_id, "ama")
    # Assigned to someone else.
    assert desk.move(case_id, "investigating", desk.yaw).status_code == 403
    assert desk.post(f"/v1/cases/{case_id}/notes", desk.yaw, {"text": "x"}).status_code == 403
    # The assigned Analyst may.
    assert desk.move(case_id, "investigating", desk.ama).status_code == 200
    assert (
        desk.post(f"/v1/cases/{case_id}/notes", desk.ama, {"text": "called her"}).status_code == 201
    )


def test_a_supervisor_can_move_and_note_any_case(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id, "ama")

    assert (
        desk.post(f"/v1/cases/{case_id}/notes", desk.esi, {"text": "checking"}).status_code == 201
    )
    assert desk.move(case_id, "investigating", desk.esi).status_code == 200
    assert desk.move(case_id, "closed_legitimate", desk.esi).status_code == 200


def test_reading_cases_is_for_analysts_and_supervisors_only(desk: Desk):
    case_id = desk.case_of(desk.alert())

    for who in (desk.admin, desk.service):
        assert desk.get("/v1/cases", who).status_code == 403
        assert desk.get(f"/v1/cases/{case_id}", who).status_code == 403
        assert desk.get("/v1/watchlist", who).status_code == 403
    for who in (desk.ama, desk.esi):
        assert desk.get("/v1/cases", who).status_code == 200
        assert desk.get(f"/v1/cases/{case_id}", who).status_code == 200
        assert desk.get("/v1/watchlist", who).status_code == 200


def test_an_analyst_sees_their_cases_and_the_unassigned_open_ones_a_supervisor_sees_all(
    desk: Desk,
):
    wallets = ["w-a", "w-b", "w-c", "w-d"]
    with desk.engine.begin() as connection:
        for w in wallets:
            records.add_wallet(
                connection, w, kyc_tier="minimum", home_region="Ashanti", registered_at=REGISTERED
            )
    mine, theirs, unassigned, closed = (desk.case_of(desk.alert(wallet=w)) for w in wallets)
    desk.assign(mine, "ama")
    desk.assign(theirs, "yaw")
    desk.assign(closed, "ama")
    desk.move(closed, "investigating", desk.esi)
    desk.move(closed, "closed_fraud", desk.esi)

    def ids(who: dict[str, str], **params: Any) -> set[int]:
        found = desk.get("/v1/cases", who, **params).json()["cases"]
        return {c["case_id"] for c in found}

    assert ids(desk.ama) == {mine, unassigned, closed}
    assert ids(desk.yaw) == {theirs, unassigned}
    assert ids(desk.esi) == {mine, theirs, unassigned, closed}
    assert ids(desk.esi, status="open") == {mine, theirs, unassigned}
    assert ids(desk.esi, assignee=SUBJECTS["yaw"]) == {theirs}
    assert ids(desk.ama, assignee="me") == {mine, closed}
    assert ids(desk.esi, wallet="w-c") == {unassigned}
    # An Analyst cannot open a Case that is not theirs or open and unassigned.
    assert desk.get(f"/v1/cases/{theirs}", desk.ama).status_code == 403
    assert desk.get(f"/v1/cases/{unassigned}", desk.ama).status_code == 200


def test_cases_are_paged_newest_first_by_cursor(desk: Desk):
    wallets = [f"w-p{n}" for n in range(5)]
    with desk.engine.begin() as connection:
        for w in wallets:
            records.add_wallet(
                connection, w, kyc_tier="minimum", home_region="Ashanti", registered_at=REGISTERED
            )
    made = [desk.case_of(desk.alert(wallet=w)) for w in wallets]

    seen: list[int] = []
    before = None
    while True:
        page = desk.get("/v1/cases", desk.esi, limit=2, **({"before": before} if before else {}))
        body_ = page.json()
        seen += [c["case_id"] for c in body_["cases"]]
        before = body_["next_before"]
        if before is None:
            break

    assert seen == sorted(made, reverse=True)


# --- the Case view -------------------------------------------------------------------------


def test_a_case_shows_its_alerts_with_reasons_and_the_wallets_recent_transactions(desk: Desk):
    alerted = desk.alert(NOW, score=88.0)
    quiet = desk.medium(NOW - HOUR, wallet=SUBJECT)
    desk.medium(NOW, wallet=OTHER)  # another Wallet's Transaction is not shown

    detail = desk.get(f"/v1/cases/{desk.case_of(alerted)}", desk.ama).json()

    [alert] = detail["alerts"]
    assert alert["transaction_id"] == alerted.transaction_id
    assert alert["assessment"]["risk_score"] == 88.0
    assert alert["assessment"]["reasons"][0]["code"] == "SIM_SWAP_DRAIN"
    assert alert["disposition"] is None
    shown = [t["transaction"]["transaction_id"] for t in detail["recent_transactions"]]
    assert shown == [alerted.transaction_id, quiet.transaction_id]
    assert detail["recent_transactions"][1]["assessment"]["risk_band"] == "Medium"


def test_a_case_that_does_not_exist_is_a_404(desk: Desk):
    assert desk.get("/v1/cases/999", desk.esi).status_code == 404


# --- transitions ---------------------------------------------------------------------------


def test_a_case_moves_open_to_investigating_to_closed(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id)

    first = desk.move(case_id, "investigating")
    last = desk.move(case_id, "closed_legitimate")

    assert first.json()["case"]["status"] == "investigating"
    assert last.json()["case"]["status"] == "closed_legitimate"
    assert last.json()["case"]["closed_by_name"] == "ama"
    moves = [
        (e["from_status"], e["to_status"])
        for e in last.json()["events"]
        if e["kind"] == "transition"
    ]
    assert moves == [("open", "investigating"), ("investigating", "closed_legitimate")]


@pytest.mark.parametrize("target", ["closed_fraud", "closed_legitimate"])
def test_an_open_case_cannot_be_closed_without_investigating(desk: Desk, target: str):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id)

    response = desk.move(case_id, target)

    assert response.status_code == 409
    assert desk.get(f"/v1/cases/{case_id}", desk.esi).json()["case"]["status"] == "open"


def test_a_case_cannot_be_moved_back_to_open_or_investigating_twice(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id)
    desk.move(case_id, "investigating")

    assert desk.move(case_id, "investigating").status_code == 409
    assert desk.move(case_id, "open").status_code == 422


@pytest.mark.parametrize("outcome", ["closed_fraud", "closed_legitimate"])
@pytest.mark.parametrize("again", ["investigating", "closed_fraud", "closed_legitimate"])
def test_a_closed_case_can_never_be_reopened_or_closed_again(desk: Desk, outcome: str, again: str):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id)
    desk.move(case_id, "investigating")
    desk.move(case_id, outcome)

    for who in (desk.ama, desk.esi):
        assert desk.move(case_id, again, who).status_code == 409
    assert desk.assign(case_id, "yaw").status_code == 409
    assert desk.post(f"/v1/cases/{case_id}/notes", desk.esi, {"text": "late"}).status_code == 409
    assert desk.get(f"/v1/cases/{case_id}", desk.esi).json()["case"]["status"] == outcome


# --- dispositions --------------------------------------------------------------------------


def test_alert_dispositions_follow_how_the_case_closed(desk: Desk):
    fraud_case = desk.case_of(desk.alert(wallet=SUBJECT))
    clean_case = desk.case_of(desk.alert(wallet=OTHER))
    for case_id, outcome in ((fraud_case, "closed_fraud"), (clean_case, "closed_legitimate")):
        desk.assign(case_id)
        desk.move(case_id, "investigating")
        before = desk.get(f"/v1/cases/{case_id}", desk.esi).json()
        assert [a["disposition"] for a in before["alerts"]] == [None]
        desk.move(case_id, outcome)

    fraud = desk.get(f"/v1/cases/{fraud_case}", desk.esi).json()
    clean = desk.get(f"/v1/cases/{clean_case}", desk.esi).json()

    assert [a["disposition"] for a in fraud["alerts"]] == ["confirmed_fraud"]
    assert [a["disposition"] for a in clean["alerts"]] == ["dismissed"]
    # Derived, not stored: no column holds it.
    with desk.engine.connect() as connection:
        columns = {c["name"] for c in sa.inspect(connection).get_columns("alerts")}
    assert not columns & {"disposition", "status"}


# --- closing as fraud ----------------------------------------------------------------------


def close_as_fraud(desk: Desk, *transactions: Transaction) -> int:
    case_id = desk.case_of(transactions[0])
    desk.assign(case_id)
    desk.move(case_id, "investigating")
    assert desk.move(case_id, "closed_fraud").status_code == 200
    return case_id


def test_closing_as_fraud_labels_the_cases_transactions_at_the_simulation_time(desk: Desk):
    one, two = desk.alert(NOW), desk.alert(NOW + HOUR)
    elsewhere = desk.alert(NOW, wallet=OTHER)
    closing = NOW + 3 * DAY
    desk.clock.advance(closing)

    close_as_fraud(desk, one, two)

    labels = desk.labels()
    assert labels == {
        one.transaction_id: (closing, "case"),
        two.transaction_id: (closing, "case"),
    }
    assert elsewhere.transaction_id not in labels
    # Known from the closing time onwards, and not before.
    known = FraudLabels(desk.engine)
    assert known.known_at(closing - timedelta(seconds=1)) == []
    assert len(known.known_at(closing)) == 2
    events = [decode(FraudDiscovered, row.payload.encode()) for row in desk.outbox()]
    assert {e.transaction_id for e in events} == {one.transaction_id, two.transaction_id}
    assert {(e.discovered_at, e.source) for e in events} == {(closing, "case")}
    assert {row.topic for row in desk.outbox()} == {"fraud.discovered"}


def test_the_closing_time_is_the_simulation_clock_not_the_wall_clock(desk: Desk):
    alerted = desk.alert(NOW)
    desk.clock.advance(NOW + 40 * DAY)  # nothing like the wall clock (2030-06-01 09:00)

    case_id = close_as_fraud(desk, alerted)

    closed_at = desk.get(f"/v1/cases/{case_id}", desk.esi).json()["case"]["closed_at"]
    assert closed_at == (NOW + 40 * DAY).isoformat().replace("+00:00", "Z")


def test_closing_as_legitimate_discovers_nothing(desk: Desk):
    alerted = desk.alert()
    case_id = desk.case_of(alerted)
    desk.assign(case_id)
    desk.move(case_id, "investigating")

    desk.move(case_id, "closed_legitimate")

    assert desk.labels() == {}
    assert desk.outbox() == []


def test_an_earlier_complaint_keeps_its_label_when_the_case_confirms_it(desk: Desk):
    alerted = desk.alert(NOW)
    FraudLabels(desk.engine).record(alerted.transaction_id, NOW + DAY, "complaint")
    desk.clock.advance(NOW + 5 * DAY)

    close_as_fraud(desk, alerted)

    assert desk.labels() == {alerted.transaction_id: (NOW + DAY, "complaint")}


def test_a_closure_that_rolls_back_leaves_no_discovery(desk: Desk):
    """The labels and the outbox rows commit with the closure or not at all."""
    alerted = desk.alert()
    case_id = desk.case_of(alerted)
    with desk.engine.begin() as connection:
        cases.assign(
            connection,
            case_id,
            assignee_id="sub-ama",
            assignee_name="ama",
            actor_id="sub-esi",
            actor_name="esi",
        )
    with desk.engine.begin() as connection:
        cases.transition(
            connection,
            case_id,
            "investigating",
            actor_id="sub-ama",
            actor_name="ama",
            supervisor=False,
        )

    with pytest.raises(RuntimeError), desk.engine.begin() as connection:
        cases.transition(
            connection,
            case_id,
            "closed_fraud",
            actor_id="sub-ama",
            actor_name="ama",
            supervisor=False,
        )
        raise RuntimeError("the request failed after the closure")

    assert desk.labels() == {}
    assert desk.outbox() == []
    assert desk.get(f"/v1/cases/{case_id}", desk.esi).json()["case"]["status"] == "investigating"


# --- the outbox ----------------------------------------------------------------------------


class Crashing(Publisher):
    """A publisher whose broker never confirms: the process 'crashes' at the flush."""

    def __init__(self) -> None:  # no producer: nothing is sent
        self.sent: list[tuple[str, bytes]] = []

    def publish_raw(
        self, topic: str, key: bytes | None, value: bytes | None, headers: Any = None
    ) -> None:
        assert value is not None
        self.sent.append((topic, value))

    def flush(self) -> None:
        raise DeliveryFailed("the broker went away")


def test_the_outbox_publishes_discoveries_and_survives_a_publisher_crash(desk: Desk, redpanda: str):
    one, two = desk.alert(NOW), desk.alert(NOW + HOUR)
    closing = NOW + 2 * DAY
    desk.clock.advance(closing)
    close_as_fraud(desk, one, two)
    topics = Topics(f"outbox{id(desk)}.")
    ensure_topics(redpanda, topics, partitions=1)

    # The publisher dies between commit and publish: nothing is lost, nothing is marked.
    with pytest.raises(DeliveryFailed):
        OutboxPublisher(desk.engine, Crashing(), topics=topics).publish_pending()
    assert [row.published for row in desk.outbox()] == [False, False]

    # A fresh publisher sends them all, once, and marks them.
    sender = OutboxPublisher(desk.engine, Publisher(redpanda), topics=topics)
    assert sender.publish_pending() == 2
    assert sender.publish_pending() == 0
    assert [row.published for row in desk.outbox()] == [True, True]

    stream = Stream(redpanda, topics, desk.database)
    messages = stream.read(topics.fraud_discoveries, 2)
    events = [decode(FraudDiscovered, m.value()) for m in messages]
    assert sorted(e.transaction_id for e in events) == sorted(
        [one.transaction_id, two.transaction_id]
    )
    assert {(e.discovered_at, e.source) for e in events} == {(closing, "case")}
    assert {m.key().decode() for m in messages} == {one.transaction_id, two.transaction_id}


def test_two_publishers_never_send_the_same_event_at_once(desk: Desk, redpanda: str):
    alerts = [desk.alert(NOW + n * HOUR) for n in range(3)]
    desk.clock.advance(NOW + DAY)
    close_as_fraud(desk, *alerts)
    topics = Topics(f"outbox2{id(desk)}.")
    ensure_topics(redpanda, topics, partitions=1)
    publishers = [
        OutboxPublisher(desk.engine, Publisher(redpanda), topics=topics) for _ in range(2)
    ]

    with ThreadPoolExecutor(2) as pool:
        sent = sum(pool.map(lambda p: p.publish_pending(), publishers))

    assert sent == 3


def test_the_app_role_may_insert_and_update_the_outbox_but_never_delete_from_it():
    assert GRANTS["outbox"] == ("SELECT", "INSERT", "UPDATE")
    assert GRANTS["cases"] == ("SELECT", "INSERT", "UPDATE")
    assert GRANTS["case_events"] == ("SELECT", "INSERT")


# --- the Medium watch list -----------------------------------------------------------------


def test_the_watch_list_holds_medium_transactions_and_no_alerts(desk: Desk):
    watched = desk.medium(NOW, wallet=SUBJECT)
    desk.alert(NOW, wallet=OTHER)  # High: an Alert, not on the watch list

    items = desk.get("/v1/watchlist", desk.ama).json()["items"]

    assert [i["transaction"]["transaction_id"] for i in items] == [watched.transaction_id]
    assert items[0]["assessment"]["risk_band"] == "Medium"
    assert items[0]["assessment"]["reasons"]


def test_the_watch_list_filters_by_wallet_region_and_transaction_type(desk: Desk):
    mine = desk.medium(NOW, wallet=SUBJECT, region="Ashanti")
    far = desk.medium(NOW + HOUR, wallet=SUBJECT, region="Volta")
    other = desk.medium(NOW + 2 * HOUR, wallet=OTHER, region="Ashanti")
    pay = desk.medium(
        NOW + 3 * HOUR,
        wallet=OTHER,
        region="Ashanti",
        transaction_type="cash_in",
        counterparty=AGENT,
    )

    def ids(**params: Any) -> list[str]:
        items = desk.get("/v1/watchlist", desk.esi, **params).json()["items"]
        return [i["transaction"]["transaction_id"] for i in items]

    assert ids(wallet=SUBJECT) == [far.transaction_id, mine.transaction_id]
    assert ids(region="Volta") == [far.transaction_id]
    assert ids(transaction_type="transfer", wallet=OTHER) == [other.transaction_id]
    assert pay.transaction_id in ids(transaction_type="cash_in")


def test_the_watch_list_is_paged_by_cursor(desk: Desk):
    made = [desk.medium(NOW + n * HOUR).transaction_id for n in range(5)]

    seen: list[str] = []
    before = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if before:
            params["before"] = before
        page = desk.get("/v1/watchlist", desk.ama, **params).json()
        seen += [i["transaction"]["transaction_id"] for i in page["items"]]
        before = page["next_before"]
        if before is None:
            break

    assert seen == made[::-1]
    assert desk.get("/v1/watchlist", desk.ama, before="not-a-cursor").status_code == 422


# --- the audit -----------------------------------------------------------------------------


def test_every_case_action_is_audited_with_who_what_and_which(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.get("/v1/cases", desk.ama)
    desk.get(f"/v1/cases/{case_id}", desk.ama)
    desk.assign(case_id, "ama")
    desk.post(f"/v1/cases/{case_id}/notes", desk.ama, {"text": "private findings"})
    desk.move(case_id, "investigating")
    desk.get("/v1/watchlist", desk.esi)

    rows = [r for r in desk.audit() if r["action"].startswith(("case.", "watchlist."))]

    assert [(r["action"], r["actor_name"], r["outcome"]) for r in rows] == [
        ("case.list", "ama", "success"),
        ("case.view", "ama", "success"),
        ("case.assign", "esi", "success"),
        ("case.note", "ama", "success"),
        ("case.transition", "ama", "success"),
        ("watchlist.list", "esi", "success"),
    ]
    assert [(r["object_type"], r["object_id"]) for r in rows[1:5]] == [("case", str(case_id))] * 4
    assert rows[3]["detail"] == {"length": len("private findings")}
    assert rows[4]["detail"] == {"from": "open", "to": "investigating", "discovered": 0}
    assert "private findings" not in json.dumps([r["detail"] for r in rows])
    assert rows[2]["actor_roles"] == ["supervisor"]


def test_denied_and_failed_case_attempts_are_audited_too(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id, "ama")
    desk.assign(case_id, "yaw", who=desk.ama)  # an Analyst assigning
    desk.move(case_id, "investigating", desk.yaw)  # not their Case
    desk.post(f"/v1/cases/{case_id}/notes", desk.yaw, {"text": "x"})
    desk.get("/v1/cases", desk.admin)
    desk.move(case_id, "closed_fraud")  # invalid: still open
    desk.get("/v1/cases/999", desk.esi)  # no such Case
    desk.assign(999, "ama")

    rows = {(r["action"], r["actor_name"], r["outcome"]) for r in desk.audit()}

    assert {
        ("case.assign", "ama", "denied"),
        ("case.transition", "yaw", "denied"),
        ("case.note", "yaw", "denied"),
        ("case.list", "kofi", "denied"),
        ("case.transition", "ama", "failed"),
        ("case.view", "esi", "failed"),
        ("case.assign", "esi", "failed"),
    } <= rows


def test_an_analyst_viewing_another_analysts_case_is_denied_and_audited(desk: Desk):
    case_id = desk.case_of(desk.alert())
    desk.assign(case_id, "yaw")

    response = desk.get(f"/v1/cases/{case_id}", desk.ama)

    assert response.status_code == 403
    [row] = desk.audit(action="case.view")
    assert (row["actor_name"], row["outcome"], row["object_id"]) == ("ama", "denied", str(case_id))


def test_an_action_that_cannot_be_audited_does_not_happen(desk: Desk):
    """The audit entry commits with the action: break the audit insert and the assignment rolls
    back with it."""
    case_id = desk.case_of(desk.alert())
    with desk.engine.begin() as connection:
        connection.execute(
            sa.text("ALTER TABLE audit_log ADD CONSTRAINT refuse CHECK (action <> 'case.assign')")
        )

    with pytest.raises(sa.exc.DBAPIError):
        desk.assign(case_id, "ama")

    case = desk.get(f"/v1/cases/{case_id}", desk.esi).json()
    assert case["case"]["assignee_id"] is None
    assert [e["kind"] for e in case["events"]] == []


# --- no raw MSISDNs ------------------------------------------------------------------------


def test_no_response_or_audit_entry_carries_a_raw_phone_number(desk: Desk):
    alerted = desk.alert()
    desk.medium(NOW)
    case_id = desk.case_of(alerted)
    desk.assign(case_id)
    desk.move(case_id, "investigating")
    desk.post(f"/v1/cases/{case_id}/notes", desk.ama, {"text": "reviewed"})
    desk.move(case_id, "closed_fraud")

    bodies = [
        desk.get("/v1/cases", desk.esi).text,
        desk.get(f"/v1/cases/{case_id}", desk.esi).text,
        desk.get("/v1/watchlist", desk.esi).text,
        json.dumps(desk.audit(), default=str),
        json.dumps([row.payload for row in desk.outbox()]),
    ]

    for text in bodies:
        assert PHONE_SHAPE.search(text) is None, PHONE_SHAPE.search(text)
