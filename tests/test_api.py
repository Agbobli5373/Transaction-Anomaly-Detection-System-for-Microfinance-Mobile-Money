"""The sync scoring API under /v1, against real Postgres, scoring with the rules alone."""

import dataclasses
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sqlalchemy as sa
from api_helpers import as_json, body, issue_key, settings, with_key
from fastapi.testclient import TestClient
from scoring_helpers import DAY, HOUR, NOW, REGISTERED, SUBJECT, routine, tx

from txrisk.api import REPLAYED_HEADER, create_app
from txrisk.features import ReplayHistory, Transaction, Wallet
from txrisk.scoring import FixedClock, RiskAssessment, ScoringService
from txrisk.store import SimulationClock, create_engine, records

FRIEND = "w-friend"
STRANGER = "w-stranger"
AGENT = "A00001"
WALLETS = (SUBJECT, FRIEND, STRANGER)


def on_app(transaction: Transaction) -> Transaction:
    return dataclasses.replace(transaction, channel="app")


class World:
    """The same hand-built history in Postgres and in an offline replay."""

    def __init__(self, database: str) -> None:
        self.settings = settings(database)
        self.engine = create_engine(database)
        self.replay = ReplayHistory(
            {w: Wallet(registered_at=REGISTERED, kyc_tier="minimum") for w in WALLETS}
        )
        with self.engine.begin() as connection:
            for wallet in WALLETS:
                records.add_wallet(
                    connection,
                    wallet,
                    kyc_tier="minimum",
                    home_region="Ashanti",
                    registered_at=REGISTERED,
                )
            records.add_agent(connection, AGENT, region="Ashanti")

    def record(self, *transactions: Transaction) -> "World":
        with self.engine.begin() as connection:
            for t in transactions:
                records.record_transaction(connection, t)
                self.replay.record(t)
        return self

    def sim_swap(self, at: Any) -> "World":
        with self.engine.begin() as connection:
            records.add_sim_swap(connection, SUBJECT, at)
        self.replay.record_sim_swap(SUBJECT, at)
        return self

    def offline(self, transaction: Transaction) -> RiskAssessment:
        service = ScoringService(
            self.replay,
            FixedClock(transaction.occurred_at),
            features=self.settings.features(),
            rules=self.settings.rules(),
        )
        return service.assess(transaction)

    def count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


@pytest.fixture
def world(database: str) -> World:
    return World(database).record(*map(on_app, routine()))


@pytest.fixture
def client(world: World, database: str) -> Iterator[TestClient]:
    """A calling service, with an API key for every scope."""
    headers = with_key(issue_key(database))
    with TestClient(create_app(world.settings), headers=headers) as client:
        yield client


def drain(at: Any = NOW) -> Transaction:
    """A Transfer of 90% of the balance to a stranger."""
    return on_app(tx(at, amount=900.0, counterparty=STRANGER, balance=1000.0))


def test_a_high_risk_transaction_raises_one_alert_and_a_repeat_replays_the_assessment(
    world: World, client: TestClient
):
    world.sim_swap(NOW - 2 * HOUR)
    transaction = drain()

    first = client.post("/v1/score", json=body(transaction))
    again = client.post("/v1/score", json=body(transaction))

    assert first.status_code == again.status_code == 200
    assert first.json()["risk_band"] == "High"
    assert first.json()["reasons"][0]["code"] == "SIM_SWAP_DRAIN"
    assert REPLAYED_HEADER not in first.headers
    assert again.headers[REPLAYED_HEADER] == "true"
    assert again.json() == first.json()
    alerts = client.get("/v1/alerts").json()["alerts"]
    assert [a["transaction_id"] for a in alerts] == [transaction.transaction_id]
    assert alerts[0]["raised_at"] == transaction.occurred_at.isoformat().replace("+00:00", "Z")
    assert alerts[0]["assessment"] == first.json()


def test_scoring_moves_the_simulation_clock_to_the_latest_event_time_and_never_back(
    world: World, client: TestClient
):
    clock = SimulationClock(world.engine)
    assert clock.peek() is None

    client.post("/v1/score", json=body(on_app(tx(NOW))))
    client.post("/v1/score", json=body(on_app(tx(NOW - HOUR))))  # an older Transaction, late

    assert clock.now() == NOW


def test_concurrent_repeats_store_one_assessment_and_one_alert(world: World, client: TestClient):
    world.sim_swap(NOW - 2 * HOUR)
    transaction = drain()

    with ThreadPoolExecutor(8) as pool:
        responses = list(
            pool.map(lambda _: client.post("/v1/score", json=body(transaction)), range(8))
        )

    assert {r.status_code for r in responses} == {200}
    assert len({str(r.json()) for r in responses}) == 1
    assert sum(REPLAYED_HEADER not in r.headers for r in responses) == 1
    assert world.count("risk_assessments") == world.count("alerts") == 1


def test_concurrent_different_transactions_under_one_id_store_one_and_conflict_the_rest(
    world: World, client: TestClient
):
    """However the calls interleave, only one Transaction is stored under an ID; every call
    with a different payload is a conflict, never a replay of someone else's assessment."""
    transaction = on_app(tx(NOW))
    bodies = [{**body(transaction), "amount": 50.0 + i} for i in range(8)]

    with ThreadPoolExecutor(8) as pool:
        responses = list(pool.map(lambda b: client.post("/v1/score", json=b), bodies))

    assert sorted(r.status_code for r in responses) == [200] + [409] * 7
    assert world.count("transactions") == len(routine()) + 1
    assert world.count("risk_assessments") == 1


def test_only_the_high_risk_band_raises_alerts(world: World, client: TestClient):
    usual = on_app(tx(NOW))  # the Subject wallet's daily 50 GHS Transfer to a friend
    unusual = on_app(tx(NOW + HOUR, amount=400.0, counterparty=STRANGER, balance=950.0))

    bands = [client.post("/v1/score", json=body(t)).json()["risk_band"] for t in (usual, unusual)]

    assert bands == ["Low", "Medium"]
    assert client.get("/v1/alerts").json() == {"alerts": [], "next_before": None}
    assert world.count("risk_assessments") == 2


def test_scores_match_the_offline_replay_whatever_order_they_arrive_in(
    world: World, client: TestClient
):
    """The clock is each Transaction's event time: one arriving late is scored against the
    history before it, not against what arrived first."""
    world.sim_swap(NOW - 2 * HOUR)
    early, late = drain(NOW), on_app(tx(NOW + HOUR, amount=50.0, balance=100.0))
    expected = {t.transaction_id: world.offline(t) for t in (early, late)}

    served = {
        t.transaction_id: client.post("/v1/score", json=body(t)).json() for t in (late, early)
    }

    assert served == {i: as_json(a) for i, a in expected.items()}


def test_a_transaction_is_returned_with_its_assessment(world: World, client: TestClient):
    transaction = on_app(tx(NOW))
    scored = client.post("/v1/score", json=body(transaction)).json()

    found = client.get(f"/v1/transactions/{transaction.transaction_id}")

    assert found.status_code == 200
    assert found.json()["assessment"] == scored
    assert found.json()["transaction"] == {
        **body(transaction),
        "occurred_at": transaction.occurred_at.isoformat().replace("+00:00", "Z"),
        "agent_id": None,
        "merchant_id": None,
        "biller_id": None,
    }
    assert client.get("/v1/transactions/T-missing").status_code == 404


def test_history_loaded_without_scoring_has_no_assessment(world: World, client: TestClient):
    history = on_app(tx(NOW - HOUR))
    world.record(history)

    found = client.get(f"/v1/transactions/{history.transaction_id}").json()

    assert found["assessment"] is None


def test_alerts_come_newest_first_a_page_at_a_time(world: World, client: TestClient):
    world.sim_swap(NOW - 2 * HOUR)
    drains = [drain(NOW + i * HOUR) for i in range(3)]
    for t in drains:
        client.post("/v1/score", json=body(t))

    first = client.get("/v1/alerts", params={"limit": 2}).json()
    rest = client.get("/v1/alerts", params={"limit": 2, "before": first["next_before"]}).json()

    ids = [a["transaction_id"] for a in first["alerts"] + rest["alerts"]]
    assert ids == [t.transaction_id for t in reversed(drains)]
    assert rest["next_before"] is None
    assert client.get("/v1/alerts", params={"subject_wallet_id": FRIEND}).json()["alerts"] == []


def test_an_unregistered_wallet_is_rejected_and_nothing_is_stored(world: World, client: TestClient):
    stranger_to_us = on_app(tx(NOW, wallet="w-unknown"))
    to_unknown = on_app(tx(NOW, counterparty="w-unknown"))

    for transaction in (stranger_to_us, to_unknown):
        response = client.post("/v1/score", json=body(transaction))
        assert response.status_code == 422
        assert "w-unknown" in response.json()["detail"]
    assert world.count("risk_assessments") == 0


def test_a_different_transaction_under_a_scored_id_is_a_conflict(client: TestClient):
    transaction = on_app(tx(NOW))
    client.post("/v1/score", json=body(transaction))

    changed = {**body(transaction), "amount": 51.0}

    assert client.post("/v1/score", json=changed).status_code == 409


@pytest.mark.parametrize(
    "change",
    [
        {"agent_id": AGENT},  # a second Counterparty
        {"counterparty_wallet_id": None},  # none at all
        {"direction": "in"},  # only a Cash-in moves money in
        {"amount": 10.001},  # more decimals than money has
        {"occurred_at": NOW.replace(tzinfo=None).isoformat()},  # no UTC offset
        {"channel": "web"},
    ],
)
def test_malformed_transactions_are_rejected(client: TestClient, change: dict[str, Any]):
    response = client.post("/v1/score", json={**body(on_app(tx(NOW))), **change})

    assert response.status_code == 422


def test_an_offset_is_converted_to_utc_before_scoring(world: World, client: TestClient):
    transaction = on_app(tx(NOW))
    posted = {**body(transaction), "occurred_at": "2030-06-01T16:00:00+02:00"}

    client.post("/v1/score", json=posted)

    found = client.get(f"/v1/transactions/{transaction.transaction_id}").json()
    assert found["transaction"]["occurred_at"] == "2030-06-01T14:00:00Z"


def test_without_a_bundle_the_rules_alone_score(client: TestClient):
    active = client.get("/v1/models/active").json()
    scored = client.post("/v1/score", json=body(on_app(tx(NOW)))).json()

    assert active == {
        "model_version": "rules-only",
        "thresholds": {"medium": 20.0, "high": 50.0},
        "fitted_on": None,
    }
    assert scored["model_version"] == "rules-only"
    assert scored["component_scores"]["anomaly"] is None


def test_health_readiness_and_the_openapi_spec_are_under_v1(client: TestClient):
    spec = client.get("/v1/openapi.json")

    assert client.get("/v1/health").json() == {"status": "ok", "detail": None}
    assert client.get("/v1/ready").json() == {
        "status": "ok",
        "detail": None,
        "degraded": False,
        "reason": None,
        "awaiting_rescore": 0,
    }
    assert spec.status_code == 200
    assert set(spec.json()["paths"]) == {
        "/v1/score",
        "/v1/transactions/{transaction_id}",
        "/v1/alerts",
        "/v1/models",
        "/v1/models/active",
        "/v1/models/rollback",
        "/v1/models/{version}/card",
        "/v1/models/{version}/evaluation",
        "/v1/models/{version}/gates",
        "/v1/models/{version}/promote",
        "/v1/audit",
        "/v1/cases",
        "/v1/cases/{case_id}",
        "/v1/cases/{case_id}/assign",
        "/v1/cases/{case_id}/notes",
        "/v1/cases/{case_id}/report",
        "/v1/cases/{case_id}/report/approve",
        "/v1/cases/{case_id}/report/export",
        "/v1/cases/{case_id}/transition",
        "/v1/watchlist",
        "/v1/api-keys",
        "/v1/api-keys/{key_id}",
        "/v1/api-keys/{key_id}/rotate",
        "/v1/health",
        "/v1/ready",
    }
    assert client.get("/openapi.json").status_code == 404


def test_the_api_never_migrates_and_is_unready_until_migrated(empty_database: str):
    with TestClient(create_app(settings(empty_database))) as client:
        health = client.get("/v1/health")
        unready = client.get("/v1/ready")

        assert health.status_code == 200
        assert unready.status_code == 503
        assert "0001_initial" in unready.json()["detail"]
    assert sa.inspect(create_engine(empty_database)).get_table_names() == []


def test_readiness_fails_when_the_database_is_unreachable():
    nothing_listens = sa.URL.create(
        "postgresql+psycopg", username="nobody", host="127.0.0.1", port=1, database="none"
    )
    unreachable = nothing_listens.render_as_string()

    with TestClient(create_app(settings(unreachable))) as client:
        response = client.get("/v1/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "database unreachable"


def test_a_cash_in_names_its_agent(world: World, client: TestClient):
    cash_in = on_app(tx(NOW - DAY / 2, "cash_in", counterparty=AGENT))

    response = client.post("/v1/score", json=body(cash_in))

    assert response.status_code == 200
    found = client.get(f"/v1/transactions/{cash_in.transaction_id}").json()
    assert found["transaction"]["agent_id"] == AGENT
    assert response.json() == as_json(world.offline(cash_in))
