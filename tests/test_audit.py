"""The audit log: append-only to the application's database role, written by every sensitive
endpoint, queryable by Admins, and free of secrets. Against real Postgres."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
import sqlalchemy as sa
from api_helpers import APP_CREDENTIAL, WallClock, as_app_role, body, issue_key, settings
from api_helpers import signed_in as bearer
from api_helpers import with_key as service
from fastapi.testclient import TestClient
from httpx import Response
from idp_helpers import LocalIdentityProvider
from scoring_helpers import NOW, REGISTERED, SUBJECT, tx
from test_clock_labels_retention import retention
from test_wall_clock import OPERATIONAL, SOURCE, clock_reads

from txrisk.api import create_app
from txrisk.cli import main
from txrisk.store import (
    APP_ROLE,
    SimulationClock,
    create_app_engine,
    create_engine,
    provision_app_role,
    purge,
    records,
)
from txrisk.store.audit import Actor, AuditEntry, record
from txrisk.store.roles import GRANTS, PASSWORD_ENV

MINUTE = timedelta(minutes=1)
MSISDN = "233241234567"
ACTOR = Actor(type="person", id="sub-1", name="ama", roles=("admin",))


def entry(**fields: Any) -> AuditEntry:
    return AuditEntry(**{"occurred_at": NOW, "actor": ACTOR, "action": "test.act", **fields})


# --- the application's database role -------------------------------------------------------


@pytest.fixture
def app_engine(database: str) -> Iterator[sa.Engine]:
    engine = create_engine(as_app_role(database))
    yield engine
    engine.dispose()


@pytest.fixture
def owner_engine(database: str) -> Iterator[sa.Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def refused_with(engine: sa.Engine, statement: str) -> str:
    """The database's message when `statement` is run and refused; fails if it is not."""
    with pytest.raises(sa.exc.DBAPIError) as refused, engine.begin() as connection:
        connection.execute(sa.text(statement))
    return str(refused.value.orig)


def test_the_app_role_can_insert_into_and_select_from_the_audit_log(app_engine: sa.Engine):
    with app_engine.begin() as connection:
        record(connection, entry(object_type="transaction", object_id="T1"))

    with app_engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT actor_id, action, object_id FROM audit_log"))
        assert [tuple(r) for r in rows] == [("sub-1", "test.act", "T1")]


def test_the_app_role_cannot_update_delete_or_truncate_audit_rows(app_engine: sa.Engine):
    with app_engine.begin() as connection:
        record(connection, entry())

    assert "permission denied" in refused_with(app_engine, "UPDATE audit_log SET action = 'x'")
    assert "permission denied" in refused_with(app_engine, "DELETE FROM audit_log")
    assert "permission denied" in refused_with(app_engine, "TRUNCATE audit_log")
    with app_engine.connect() as connection:
        assert connection.execute(sa.text("SELECT count(*) FROM audit_log")).scalar_one() == 1


def test_the_app_role_cannot_alter_drop_or_disable_the_audit_log(app_engine: sa.Engine):
    for statement in (
        "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        "ALTER TABLE audit_log ADD COLUMN extra text",
        "DROP TRIGGER audit_log_no_row_change ON audit_log",
        "DROP TABLE audit_log",
        "CREATE TABLE sneaky (id int)",
    ):
        message = refused_with(app_engine, statement)
        assert "must be owner" in message or "permission denied" in message, statement


def test_triggers_refuse_update_delete_and_truncate_even_for_the_owner(owner_engine: sa.Engine):
    with owner_engine.begin() as connection:
        record(connection, entry())

    for statement in ("UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        assert "audit_log is append-only" in refused_with(owner_engine, statement)
    assert "audit_log is append-only" in refused_with(owner_engine, "TRUNCATE audit_log")


def test_the_app_role_holds_exactly_the_decided_grants(
    database: str, owner_engine: sa.Engine, app_engine: sa.Engine
):
    with owner_engine.connect() as connection:
        granted = connection.execute(
            sa.text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = :role"
            ),
            {"role": APP_ROLE},
        )
        held: dict[str, set[str]] = {}
        for table, privilege in granted:
            held.setdefault(table, set()).add(privilege)
        role = connection.execute(
            sa.text("SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = :r"),
            {"r": APP_ROLE},
        ).one()

    assert held == {table: set(privileges) for table, privileges in GRANTS.items()}
    assert held["audit_log"] == {"SELECT", "INSERT"}
    assert tuple(role) == (False, False, False)


def test_a_table_without_decided_grants_stops_provisioning(owner_engine: sa.Engine):
    with owner_engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE undecided (id int)"))

    with pytest.raises(RuntimeError, match="undecided"):
        provision_app_role(owner_engine, APP_CREDENTIAL)


def test_the_app_engine_takes_the_role_credential_from_the_environment(database: str):
    url = sa.make_url(database)
    # No password in the URL: the engine adds the role's from the environment.
    url = sa.URL.create(
        url.drivername, username=APP_ROLE, host=url.host, port=url.port, database=url.database
    ).render_as_string(hide_password=False)

    engine = create_app_engine(url, {PASSWORD_ENV: APP_CREDENTIAL})

    with engine.connect() as connection:
        assert connection.execute(sa.text("SELECT current_user")).scalar_one() == APP_ROLE


def test_txrisk_migrate_provisions_the_role_only_when_its_credential_is_set(
    empty_database: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    assert main(["migrate", "--database-url", empty_database]) == 0
    assert "was not provisioned" in capsys.readouterr().err

    monkeypatch.setenv(PASSWORD_ENV, APP_CREDENTIAL)
    assert main(["migrate", "--database-url", empty_database]) == 0
    assert f"provisioned the {APP_ROLE} database role" in capsys.readouterr().out
    engine = create_engine(as_app_role(empty_database))
    with engine.begin() as connection:
        record(connection, entry())
    engine.dispose()


def test_the_operator_command_that_issues_a_key_is_audited(
    database: str, capsys: pytest.CaptureFixture[str]
):
    issue = ["api-key", "--name", "ledger", "--scope", "alerts:read", "--database-url", database]
    assert main(issue) == 0

    key = capsys.readouterr().out.strip()
    [row] = audit_rows(database)
    assert (row["actor_type"], row["action"], row["outcome"]) == (
        "operator",
        "api_key.issue",
        "success",
    )
    assert row["detail"]["scopes"] == ["alerts:read"]
    assert key not in json.dumps(row, default=str)


# --- retention: the one way an entry can leave -------------------------------------------------


def aged_entries(engine: sa.Engine) -> None:
    """Entries 1, 400 and 3000 days old, on the wall clock the log is stamped with."""
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for days in (1, 400, 3000):
            record(
                connection,
                entry(occurred_at=now - timedelta(days=days), object_type="t", object_id=str(days)),
            )


def remaining(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT object_id FROM audit_log ORDER BY occurred_at"))
        return [row[0] for row in rows]


def test_old_entries_leave_only_through_the_purge_and_only_past_the_period(owner_engine):
    aged_entries(owner_engine)

    # No DELETE works on its own, whatever the age.
    for days in (1, 400, 3000):
        message = refused_with(owner_engine, f"DELETE FROM audit_log WHERE object_id = '{days}'")
        assert "audit_log is append-only" in message
    with owner_engine.begin() as connection:
        removed = connection.execute(
            sa.text("SELECT audit_log_purge(make_interval(days => 2555))")
        ).scalar_one()

    assert removed == 1
    assert remaining(owner_engine) == ["400", "1"]


def test_the_purge_refuses_a_period_under_a_year(owner_engine):
    aged_entries(owner_engine)

    message = refused_with(owner_engine, "SELECT audit_log_purge(make_interval(days => 30))")

    assert "kept at least 365 days" in message
    assert remaining(owner_engine) == ["3000", "400", "1"]


def test_a_forged_purge_flag_still_cannot_delete_a_recent_entry(owner_engine):
    aged_entries(owner_engine)
    forged = "SELECT set_config('txrisk.audit_purge', 'on', true); DELETE FROM audit_log WHERE "

    # Younger than the one-year floor: refused even with the flag set.
    assert "append-only" in refused_with(owner_engine, forged + "object_id = '1'")
    assert remaining(owner_engine) == ["3000", "400", "1"]


def test_the_app_role_cannot_run_the_audit_purge(app_engine: sa.Engine, owner_engine: sa.Engine):
    aged_entries(owner_engine)

    message = refused_with(app_engine, "SELECT audit_log_purge(make_interval(days => 400))")

    assert "permission denied" in message
    assert remaining(owner_engine) == ["3000", "400", "1"]


def test_txrisk_purge_deletes_aged_audit_entries_and_keeps_the_rest(owner_engine: sa.Engine):
    aged_entries(owner_engine)
    clock = SimulationClock(owner_engine)
    clock.advance(NOW)

    purged = purge(owner_engine, clock, retention(audit_log=500))

    assert purged is not None and purged.deleted["audit_log"] == 1
    assert remaining(owner_engine) == ["400", "1"]


def test_txrisk_purge_refuses_to_run_as_the_app_role(database: str, capsys):
    exit_code = main(["purge", "--database-url", as_app_role(database)])

    assert exit_code == 2
    assert "run it as the owner" in capsys.readouterr().err


# --- the wall clock ---------------------------------------------------------------------------


def test_the_audit_log_reads_the_wall_clock_only_where_the_allow_list_says():
    """The audit modules take their time from the caller, so they read no clock themselves; the
    clock the API hands them, and the database's retention floor, are in the allow-list."""
    for module in ("store/audit.py", "api/audit.py", "store/roles.py"):
        assert clock_reads((SOURCE / module).read_text(encoding="utf-8")) == [], module
    reasons = {use: why for use, why in OPERATIONAL.items() if "audit" in why}
    assert {"api/app.py", "store/migrations/0004_audit_log.sql"} <= {f for f, _ in reasons}


# --- the endpoints --------------------------------------------------------------------------


def audit_rows(database: str, where: str = "true") -> list[dict[str, Any]]:
    """Every audit row as the owner sees it, oldest first."""
    engine = create_engine(database)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(f"SELECT * FROM audit_log WHERE {where} ORDER BY audit_id")
        )
        found = [row._asdict() for row in rows]
    engine.dispose()
    return found


def subject_of(access_token: str) -> str:
    claims = jwt.decode(access_token, options={"verify_signature": False})
    return str(claims["sub"])


class Api:
    """The API, called by an Analyst (ama), an Admin (kofi) and services, on a hand-moved clock."""

    def __init__(
        self, database: str, client: TestClient, idp: LocalIdentityProvider, clock: WallClock
    ) -> None:
        self.database = database
        self.client = client
        self.clock = clock
        self.analyst_token = idp.access("analyst", preferred_username="ama")
        self.admin_token = idp.access("admin", preferred_username="kofi")
        self.analyst = bearer(self.analyst_token)
        self.admin = bearer(self.admin_token)
        self.analyst_id = subject_of(self.analyst_token)
        self.admin_id = subject_of(self.admin_token)

    def entries(self, **where: str) -> list[dict[str, Any]]:
        clause = " AND ".join(f"{column} = '{value}'" for column, value in where.items())
        return audit_rows(self.database, clause or "true")

    def issue(self, *scopes: str, **options: Any) -> dict[str, Any]:
        response = self.client.post(
            "/v1/api-keys",
            json={"name": "ledger", "scopes": list(scopes), **options},
            headers=self.admin,
        )
        assert response.status_code == 201, response.text
        issued: dict[str, Any] = response.json()
        return issued

    def audit(self, **params: Any) -> Response:
        return self.client.get("/v1/audit", params=params, headers=self.admin)


@pytest.fixture
def api(database: str, local_idp: LocalIdentityProvider) -> Iterator[Api]:
    clock = WallClock()
    engine = create_engine(database)
    with engine.begin() as connection:
        for wallet in (SUBJECT, "w-friend"):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )
    engine.dispose()
    app = create_app(
        settings(database, oidc_issuer=local_idp.issuer, oidc_jwks_url=local_idp.jwks_url),
        wall_clock=clock,
    )
    with TestClient(app) as client:
        yield Api(database, client, local_idp, clock)


def stored_transaction(api: Api) -> str:
    """Score one Transaction as a service, so there is one to view; returns its ID."""
    transaction = dataclasses.replace(tx(), channel="app")
    key = issue_key(api.database, "score:write")
    response = api.client.post("/v1/score", json=body(transaction), headers=service(key))
    assert response.status_code == 200, response.text
    return transaction.transaction_id


def test_viewing_a_transaction_records_who_what_which_and_when(api: Api):
    transaction_id = stored_transaction(api)
    api.clock.advance(7 * MINUTE)

    response = api.client.get(f"/v1/transactions/{transaction_id}", headers=api.analyst)

    assert response.status_code == 200
    [row] = api.entries(action="transaction.view")
    assert row["actor_type"] == "person"
    assert row["actor_id"] == api.analyst_id
    assert row["actor_name"] == "ama"
    assert row["actor_roles"] == ["analyst"]
    assert (row["object_type"], row["object_id"]) == ("transaction", transaction_id)
    assert row["outcome"] == "success"
    assert row["occurred_at"] == datetime(2030, 6, 1, 9, 7, tzinfo=UTC)


def test_a_service_viewing_a_transaction_is_recorded_under_its_key_id(api: Api):
    transaction_id = stored_transaction(api)
    key = api.issue("transactions:read")

    api.client.get(f"/v1/transactions/{transaction_id}", headers=service(key["key"]))

    [row] = api.entries(action="transaction.view")
    assert row["actor_type"] == "service"
    assert row["actor_id"] == key["api_key"]["key_id"]
    assert row["actor_roles"] == []


def test_viewing_a_missing_transaction_is_recorded_as_failed(api: Api):
    response = api.client.get("/v1/transactions/T-missing", headers=api.analyst)

    assert response.status_code == 404
    [row] = api.entries(action="transaction.view")
    assert (row["object_id"], row["outcome"]) == ("T-missing", "failed")


def test_listing_alerts_is_recorded_with_the_filter_used(api: Api):
    response = api.client.get(
        "/v1/alerts", params={"subject_wallet_id": SUBJECT, "limit": 5}, headers=api.analyst
    )

    assert response.status_code == 200
    [row] = api.entries(action="alert.list")
    assert (row["actor_id"], row["object_type"], row["object_id"]) == (
        api.analyst_id,
        "alert",
        None,
    )
    assert row["detail"] == {
        "limit": 5,
        "before": None,
        "subject_wallet_id": SUBJECT,
        "returned": 0,
    }


def test_issuing_rotating_and_revoking_keys_record_which_key_and_never_the_secret(api: Api):
    issued = api.issue("alerts:read", "transactions:read")
    key_id = issued["api_key"]["key_id"]
    rotated = api.client.post(f"/v1/api-keys/{key_id}/rotate", headers=api.admin).json()
    new_id = rotated["api_key"]["key_id"]
    revoked = api.client.delete(f"/v1/api-keys/{new_id}", headers=api.admin)
    api.client.get("/v1/api-keys", headers=api.admin)

    assert revoked.status_code == 200
    issue, rotate, revoke, listing = api.entries()
    assert (issue["action"], issue["object_type"], issue["object_id"]) == (
        "api_key.issue",
        "api_key",
        key_id,
    )
    assert issue["actor_id"] == api.admin_id
    assert issue["detail"]["scopes"] == ["alerts:read", "transactions:read"]
    assert (rotate["action"], rotate["object_id"]) == ("api_key.rotate", key_id)
    assert rotate["detail"]["replaces"] == key_id
    assert (revoke["action"], revoke["object_id"]) == ("api_key.revoke", new_id)
    assert listing["action"] == "api_key.list"
    assert {r["outcome"] for r in (issue, rotate, revoke, listing)} == {"success"}


def test_an_admin_issuing_themselves_a_read_key_leaves_a_trail(api: Api):
    key = api.issue("transactions:read")
    transaction_id = stored_transaction(api)

    api.client.get(f"/v1/transactions/{transaction_id}", headers=service(key["key"]))

    issue, view = api.entries()
    assert issue["actor_id"] == api.admin_id and issue["actor_name"] == "kofi"
    assert issue["object_id"] == key["api_key"]["key_id"]
    assert view["actor_id"] == key["api_key"]["key_id"]
    assert key["api_key"]["created_by"] == "kofi"


def test_failed_key_operations_are_recorded(api: Api):
    key_id = api.issue("alerts:read")["api_key"]["key_id"]
    api.client.post(f"/v1/api-keys/{key_id}/rotate", headers=api.admin)

    again = api.client.post(f"/v1/api-keys/{key_id}/rotate", headers=api.admin)
    unknown = api.client.delete("/v1/api-keys/k-none", headers=api.admin)

    assert (again.status_code, unknown.status_code) == (409, 404)
    failed = api.entries(outcome="failed")
    assert [(r["action"], r["object_id"]) for r in failed] == [
        ("api_key.rotate", key_id),
        ("api_key.revoke", "k-none"),
    ]


def test_denied_attempts_are_recorded(api: Api):
    scoring_only = api.issue("score:write")

    responses = [
        api.client.post(
            "/v1/api-keys", json={"name": "x", "scopes": ["alerts:read"]}, headers=api.analyst
        ),
        api.client.get("/v1/api-keys", headers=api.analyst),
        api.client.post("/v1/api-keys/k-1/rotate", headers=api.analyst),
        api.client.delete("/v1/api-keys/k-1", headers=api.analyst),
        api.client.get("/v1/transactions/T-1", headers=api.admin),
        api.client.get("/v1/alerts", headers=service(scoring_only["key"])),
        api.client.get("/v1/audit", headers=api.analyst),
    ]

    assert {r.status_code for r in responses} == {403}
    denied = api.entries(outcome="denied")
    assert [(r["action"], r["object_id"]) for r in denied] == [
        ("api_key.issue", None),
        ("api_key.list", None),
        ("api_key.rotate", "k-1"),
        ("api_key.revoke", "k-1"),
        ("transaction.view", "T-1"),
        ("alert.list", None),
        ("audit.read", None),
    ]
    assert [r["actor_roles"] for r in denied[:4]] == [["analyst"]] * 4
    assert denied[4]["actor_roles"] == ["admin"]
    assert denied[5]["actor_type"] == "service"
    assert denied[5]["actor_id"] == scoring_only["api_key"]["key_id"]


def test_scoring_calls_are_not_audited(api: Api):
    stored_transaction(api)
    key = issue_key(api.database, "score:write")

    api.client.post(
        "/v1/score",
        json=body(dataclasses.replace(tx(NOW + MINUTE), channel="app")),
        headers=service(key),
    )

    assert api.entries() == []


def test_unauthenticated_requests_are_not_audited(api: Api):
    assert api.client.get("/v1/alerts").status_code == 401

    assert api.entries() == []


def test_a_request_that_cannot_be_audited_returns_nothing(api: Api, database: str):
    transaction_id = stored_transaction(api)
    owner = create_engine(database)
    with owner.begin() as connection:
        connection.execute(sa.text("REVOKE INSERT ON audit_log FROM txrisk_app"))
    app = api.client.app
    assert app is not None

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/v1/transactions/{transaction_id}", headers=api.analyst)

    assert response.status_code == 500
    assert transaction_id not in response.text
    owner.dispose()


# --- querying it ----------------------------------------------------------------------------


@pytest.fixture
def busy(api: Api) -> Api:
    """An audit log with a known history: five minutes apart, from two actors."""
    transaction_id = stored_transaction(api)
    steps = (
        ("alerts", api.analyst, "/v1/alerts"),
        ("view", api.analyst, f"/v1/transactions/{transaction_id}"),
        ("view-missing", api.analyst, "/v1/transactions/T-missing"),
        ("keys", api.admin, "/v1/api-keys"),
        ("denied", api.analyst, "/v1/api-keys"),
    )
    for _, headers, path in steps:
        api.clock.advance(5 * MINUTE)
        api.client.get(path, headers=headers)
    return api


def actions(response: Response) -> list[str]:
    assert response.status_code == 200, response.text
    return [e["action"] for e in response.json()["entries"]]


def test_the_audit_log_is_read_newest_first_by_admins(busy: Api):
    response = busy.audit()

    assert actions(response) == [
        "api_key.list",
        "api_key.list",
        "transaction.view",
        "transaction.view",
        "alert.list",
    ]
    first = response.json()["entries"][0]
    assert first["outcome"] == "denied" and first["actor_roles"] == ["analyst"]


def test_the_audit_log_filters_by_actor_action_object_outcome_and_time(busy: Api):
    by_admin = busy.audit(actor=busy.admin_id)
    views = busy.audit(action="transaction.view")
    one_object = busy.audit(object_type="transaction", object_id="T-missing")
    denied = busy.audit(outcome="denied")
    services = busy.audit(actor_type="service")
    # Entries at 09:05 (alerts), :10 (view), :15 (view-missing), :20 (keys), :25 (denied).
    window = busy.audit(since="2030-06-01T09:10:00Z", until="2030-06-01T09:20:00Z")

    assert actions(by_admin) == ["api_key.list"]
    assert actions(views) == ["transaction.view"] * 2
    assert actions(one_object) == ["transaction.view"]
    assert one_object.json()["entries"][0]["outcome"] == "failed"
    assert actions(denied) == ["api_key.list"]
    assert actions(services) == []
    assert actions(window) == ["transaction.view", "transaction.view"]


def test_the_audit_log_pages_with_a_cursor(busy: Api):
    first = busy.audit(limit=2).json()
    second = busy.audit(limit=2, before=first["next_before"]).json()
    third = busy.audit(limit=2, before=second["next_before"]).json()

    ids = [e["audit_id"] for page in (first, second, third) for e in page["entries"]]
    assert len(ids) == 5 and ids == sorted(ids, reverse=True) and len(set(ids)) == 5
    assert third["next_before"] is None


def test_reading_the_audit_log_is_itself_audited(busy: Api):
    busy.audit(actor=busy.analyst_id, action="alert.list")

    [row] = busy.entries(action="audit.read")
    assert row["actor_id"] == busy.admin_id
    assert (row["object_type"], row["outcome"]) == ("audit_log", "success")
    assert row["detail"]["filters"] == {
        "actor": busy.analyst_id,
        "action": "alert.list",
        "limit": 50,
    }
    assert "audit.read" in actions(busy.audit())


def test_a_service_key_cannot_read_the_audit_log(busy: Api):
    every_scope = issue_key(busy.database)

    response = busy.client.get("/v1/audit", headers=service(every_scope))

    assert response.status_code == 403


# --- what never gets stored -----------------------------------------------------------------


def test_no_secret_or_phone_number_is_ever_stored_in_an_entry(api: Api):
    issued = api.issue("alerts:read", "transactions:read")
    rotated = api.client.post(
        f"/v1/api-keys/{issued['api_key']['key_id']}/rotate", headers=api.admin
    ).json()
    api.client.delete(f"/v1/api-keys/{rotated['api_key']['key_id']}", headers=api.admin)
    api.client.get(f"/v1/transactions/{MSISDN}", headers=api.analyst)
    api.client.get(f"/v1/transactions/+{MSISDN}", headers=api.admin)
    api.client.get(f"/v1/transactions/{MSISDN}", headers=service(issued["key"]))
    api.client.get("/v1/audit", headers=api.admin)

    stored = json.dumps(audit_rows(api.database), default=str)

    assert len(audit_rows(api.database)) >= 7
    secrets_and_pii = [
        issued["key"],
        issued["key"].split("_", 2)[2],
        rotated["key"],
        rotated["key"].split("_", 2)[2],
        api.analyst_token,
        api.admin_token,
        MSISDN,
    ]
    for secret in secrets_and_pii:
        assert secret not in stored
    typed = audit_rows(api.database, "object_id = '[redacted]'")
    assert [r["outcome"] for r in typed] == ["failed", "denied", "failed"]
