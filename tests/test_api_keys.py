"""API keys: Admins issue, rotate and revoke them over /v1, and only their hashes are stored."""

import hashlib
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from api_helpers import WallClock, settings, signed_in, with_key
from fastapi.testclient import TestClient
from httpx import Response
from idp_helpers import LocalIdentityProvider

from txrisk.api import create_app
from txrisk.cli import main
from txrisk.store import create_engine

MINUTE = timedelta(minutes=1)


@pytest.fixture
def wall_clock() -> WallClock:
    return WallClock()


class Admin:
    """The API, called by an Admin signed in as ama, or by the services holding its keys."""

    def __init__(self, client: TestClient, local_idp: LocalIdentityProvider) -> None:
        self.client = client
        self.headers = signed_in(local_idp.access("admin", preferred_username="ama"))

    def post(self, path: str, json: object = None) -> Response:
        return self.client.post(path, json=json, headers=self.headers)

    def get(self, path: str) -> Response:
        return self.client.get(path, headers=self.headers)

    def delete(self, path: str) -> Response:
        return self.client.delete(path, headers=self.headers)

    def issue(self, *scopes: str, **options: object) -> dict[str, Any]:
        response = self.post("/v1/api-keys", {"name": "ledger", "scopes": list(scopes), **options})
        assert response.status_code == 201, response.text
        issued: dict[str, Any] = response.json()
        return issued

    def rotate(self, key_id: str, json: object = None) -> Response:
        return self.post(f"/v1/api-keys/{key_id}/rotate", json)

    def status_with(self, key: str, path: str = "/v1/alerts") -> int:
        """Call as the service holding `key`."""
        return self.client.get(path, headers=with_key(key)).status_code


@pytest.fixture
def admin(
    database: str, local_idp: LocalIdentityProvider, wall_clock: WallClock
) -> Iterator[Admin]:
    app = create_app(
        settings(database, oidc_issuer=local_idp.issuer, oidc_jwks_url=local_idp.jwks_url),
        wall_clock=wall_clock,
    )
    with TestClient(app) as client:
        yield Admin(client, local_idp)


def test_an_issued_key_works_for_its_scopes_only(admin: Admin, wall_clock: WallClock):
    issued = admin.issue("alerts:read")

    assert issued["api_key"] == {
        "key_id": issued["api_key"]["key_id"],
        "name": "ledger",
        "scopes": ["alerts:read"],
        "rate_limit_per_minute": 600,
        "created_at": "2030-06-01T09:00:00Z",
        "created_by": "ama",
        "expires_at": None,
        "revoked_at": None,
        "replaces": None,
        "active": True,
    }
    assert admin.status_with(issued["key"]) == 200
    assert admin.status_with(issued["key"], "/v1/models/active") == 403


def test_keys_are_stored_only_as_hashes(admin: Admin, database: str):
    key = admin.issue("score:write", "alerts:read")["key"]

    with create_engine(database).connect() as connection:
        row = connection.execute(sa.text("SELECT * FROM api_keys")).one()._asdict()

    assert row["key_digest"] == hashlib.sha256(key.encode()).digest()
    random_part = key.rsplit("_", 1)[1]
    assert not any(random_part in str(value) for value in row.values())


def test_listing_keys_never_shows_the_keys(admin: Admin):
    key = admin.issue("alerts:read")["key"]

    listed = admin.get("/v1/api-keys")

    assert listed.status_code == 200
    assert [k["scopes"] for k in listed.json()] == [["alerts:read"]]
    assert key.rsplit("_", 1)[1] not in listed.text
    assert "digest" not in listed.text


def test_rotation_keeps_both_keys_working_through_the_overlap_then_only_the_new(
    admin: Admin, wall_clock: WallClock
):
    old = admin.issue("alerts:read", rate_limit_per_minute=120)
    old_id = old["api_key"]["key_id"]

    wall_clock.advance(MINUTE)
    rotated = admin.rotate(old_id, {"overlap_minutes": 30})

    assert rotated.status_code == 201
    new = rotated.json()
    assert new["key"] != old["key"]
    assert new["api_key"]["replaces"] == old_id
    assert new["api_key"]["scopes"] == ["alerts:read"]
    assert new["api_key"]["rate_limit_per_minute"] == 120
    assert new["api_key"]["expires_at"] is None
    assert admin.status_with(old["key"]) == admin.status_with(new["key"]) == 200

    wall_clock.advance(29 * MINUTE)
    assert admin.status_with(old["key"]) == 200

    wall_clock.advance(MINUTE)
    assert admin.status_with(old["key"]) == 401
    assert admin.status_with(new["key"]) == 200
    listed = {k["key_id"]: k for k in admin.get("/v1/api-keys").json()}
    assert listed[old_id]["expires_at"] == "2030-06-01T09:31:00Z"
    assert listed[old_id]["active"] is False


def test_rotation_overlaps_for_a_day_by_default_and_zero_ends_the_old_key_at_once(
    admin: Admin, wall_clock: WallClock
):
    first = admin.issue("alerts:read")
    second = admin.rotate(first["api_key"]["key_id"]).json()
    third = admin.rotate(second["api_key"]["key_id"], {"overlap_minutes": 0}).json()

    assert admin.status_with(first["key"]) == 200
    assert admin.status_with(second["key"]) == 401
    assert admin.status_with(third["key"]) == 200
    wall_clock.advance(24 * 60 * MINUTE)
    assert admin.status_with(first["key"]) == 401


def test_a_revoked_key_is_refused_at_once_and_cannot_be_rotated(admin: Admin):
    issued = admin.issue("alerts:read")
    key_id = issued["api_key"]["key_id"]

    revoked = admin.delete(f"/v1/api-keys/{key_id}")

    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] == "2030-06-01T09:00:00Z"
    assert revoked.json()["active"] is False
    assert admin.status_with(issued["key"]) == 401
    assert admin.rotate(key_id).status_code == 409


def test_a_key_stops_working_when_it_expires(admin: Admin, wall_clock: WallClock):
    issued = admin.issue("alerts:read", expires_at="2030-06-01T10:00:00Z")

    assert admin.status_with(issued["key"]) == 200
    wall_clock.advance(60 * MINUTE)
    assert admin.status_with(issued["key"]) == 401


def test_rotating_or_revoking_an_unknown_key_is_not_found(admin: Admin):
    assert admin.rotate("0000000000000000").status_code == 404
    assert admin.delete("/v1/api-keys/0000000000000000").status_code == 404


@pytest.mark.parametrize(
    "request_body",
    [
        {"name": "ledger", "scopes": []},
        {"name": "ledger", "scopes": ["alerts:write"]},
        {"name": "ledger", "scopes": ["alerts:read"], "rate_limit_per_minute": 0},
        {"name": "", "scopes": ["alerts:read"]},
    ],
    ids=["no scope", "unknown scope", "no requests allowed", "no name"],
)
def test_a_malformed_key_request_is_rejected(admin: Admin, request_body: dict[str, object]):
    assert admin.post("/v1/api-keys", request_body).status_code == 422


def test_txrisk_api_key_issues_a_working_key(
    database: str, admin: Admin, capsys: pytest.CaptureFixture[str]
):
    issue = ["api-key", "--name", "ledger", "--scope", "alerts:read", "--database-url", database]
    assert main(issue) == 0

    key = capsys.readouterr().out.strip()
    assert admin.status_with(key) == 200
    assert admin.status_with(key, "/v1/models/active") == 403
    unknown_scope = ["api-key", "--name", "x", "--scope", "everything", "--database-url", database]
    assert main(unknown_scope) == 2


def test_rotation_hands_on_the_expiry_rather_than_lifting_it(admin: Admin, wall_clock: WallClock):
    expiring = admin.issue("alerts:read", expires_at="2030-06-01T12:00:00Z")

    successor = admin.rotate(expiring["api_key"]["key_id"], {"overlap_minutes": 30}).json()

    assert successor["api_key"]["expires_at"] == "2030-06-01T12:00:00Z"
    wall_clock.advance(3 * 60 * MINUTE)
    assert admin.status_with(expiring["key"]) == 401
    assert admin.status_with(successor["key"]) == 401


def test_a_key_is_rotated_once_so_only_one_successor_is_ever_live(admin: Admin):
    original = admin.issue("alerts:read")
    key_id = original["api_key"]["key_id"]
    successor = admin.rotate(key_id).json()

    again = admin.rotate(key_id)

    assert again.status_code == 409
    assert again.json() == {
        "detail": "the API key has already been rotated; rotate its replacement"
    }
    listed = admin.get("/v1/api-keys").json()
    assert len(listed) == 2
    assert [k["key_id"] for k in listed if k["replaces"] == key_id] == [
        successor["api_key"]["key_id"]
    ]
    assert admin.rotate(successor["api_key"]["key_id"]).status_code == 201
