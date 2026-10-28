"""Who may call what under /v1: each role, signed in through real Keycloak, and each API key
scope, allowed and denied on every endpoint, against real Postgres."""

import urllib.error
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest
from api_helpers import body, issue_key, settings, signed_in, with_key
from conftest import Keycloak
from fastapi.testclient import TestClient
from httpx import Response
from scoring_helpers import NOW, tx

from txrisk.api import create_app
from txrisk.api.auth import SCOPES
from txrisk.api.signin import demo_access

# Every /v1 endpoint behind a credential, as a request that reaches it once permitted.
ENDPOINTS: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "score": ("POST", "/v1/score", body(replace(tx(NOW), channel="app"))),
    "read a transaction": ("GET", "/v1/transactions/T-none", None),
    "read alerts": ("GET", "/v1/alerts", None),
    "read the active model": ("GET", "/v1/models/active", None),
    "read the audit log": ("GET", "/v1/audit", None),
    "list cases": ("GET", "/v1/cases", None),
    "read a case": ("GET", "/v1/cases/1", None),
    "assign a case": ("POST", "/v1/cases/1/assign", {"assignee_id": "sub-ama"}),
    "add a case note": ("POST", "/v1/cases/1/notes", {"text": "checked"}),
    "move a case": ("POST", "/v1/cases/1/transition", {"to": "investigating"}),
    "read the watch list": ("GET", "/v1/watchlist", None),
    "list api keys": ("GET", "/v1/api-keys", None),
    "issue an api key": ("POST", "/v1/api-keys", {"name": "svc", "scopes": ["alerts:read"]}),
    "rotate an api key": ("POST", "/v1/api-keys/k-none/rotate", None),
    "revoke an api key": ("DELETE", "/v1/api-keys/k-none", None),
}
# The clients compose allows: the workbench and the dev-only command-line sign-in.
COMPOSE_CLIENTS = ("txrisk-workbench", "txrisk-demo-cli")
API_KEYS = {"list api keys", "issue an api key", "rotate an api key", "revoke an api key"}
# What no API key scope reaches: only people, and only Admins.
ADMIN_ONLY = {*API_KEYS, "read the audit log"}
# Cases and the watch list are for the people who work them; no API key scope reaches them.
CASE_WORK = {"list cases", "read a case", "add a case note", "move a case", "read the watch list"}
PEOPLE_ONLY = {*CASE_WORK, "assign a case"}

# What each role may do; nothing else.
ROLE_ACCESS = {
    "analyst": {"read a transaction", "read alerts", "read the active model", *CASE_WORK},
    "supervisor": {
        "read a transaction",
        "read alerts",
        "read the active model",
        *PEOPLE_ONLY,
    },
    "admin": {"read the active model", *ADMIN_ONLY},
}
# What each API key scope allows; nothing else. No scope reaches the API key endpoints or the
# audit log.
SCOPE_ACCESS = {
    "score:write": {"score"},
    "transactions:read": {"read a transaction"},
    "alerts:read": {"read alerts"},
    "models:read": {"read the active model"},
}


def call(client: TestClient, endpoint: str, headers: dict[str, str] | None = None) -> Response:
    method, path, json = ENDPOINTS[endpoint]
    return client.request(method, path, json=json, headers=headers)


def allowed(response: Response) -> bool:
    """Past authentication and authorisation, whatever the endpoint then answered (a 404 for
    the missing Transaction, a 422 for the unregistered Wallet)."""
    assert response.status_code != 401, response.text
    return response.status_code != 403


@pytest.fixture
def client(database: str, keycloak: Keycloak) -> Iterator[TestClient]:
    app = create_app(
        settings(database, oidc_issuer=keycloak.realm_url, oidc_allowed_clients=COMPOSE_CLIENTS)
    )
    with TestClient(app) as client:
        yield client


def test_the_scopes_cover_every_endpoint_but_the_admin_and_people_only_ones():
    assert set(SCOPE_ACCESS) == set(SCOPES)
    assert set().union(*SCOPE_ACCESS.values()) == set(ENDPOINTS) - ADMIN_ONLY - PEOPLE_ONLY


@pytest.mark.parametrize("role", sorted(ROLE_ACCESS))
def test_each_role_signed_in_through_keycloak_reaches_only_its_endpoints(
    client: TestClient, keycloak: Keycloak, role: str
):
    headers = signed_in(demo_access(keycloak.realm_url, role, sign_in=keycloak.sign_ins[role]))

    reached = {e for e in ENDPOINTS if allowed(call(client, e, headers))}

    assert reached == ROLE_ACCESS[role]


@pytest.mark.parametrize("scope", sorted(SCOPE_ACCESS))
def test_each_api_key_scope_reaches_only_its_endpoints(
    client: TestClient, database: str, scope: str
):
    headers = with_key(issue_key(database, scope))

    reached = {e for e in ENDPOINTS if allowed(call(client, e, headers))}

    assert reached == SCOPE_ACCESS[scope]


def test_a_key_with_every_scope_still_cannot_manage_api_keys(client: TestClient, database: str):
    headers = with_key(issue_key(database))

    for endpoint in API_KEYS:
        response = call(client, endpoint, headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "not permitted to issue, rotate and revoke API keys"}


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_every_v1_endpoint_needs_a_credential(client: TestClient, endpoint: str):
    response = call(client, endpoint)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")


def test_health_readiness_and_the_spec_stay_open(client: TestClient):
    for path in ("/v1/health", "/v1/ready", "/v1/openapi.json", "/v1/docs"):
        assert client.get(path).status_code == 200, path


def test_the_spec_documents_both_ways_to_authenticate(client: TestClient):
    spec = client.get("/v1/openapi.json").json()

    schemes = spec["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    assert schemes["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "A scoped API key, for services.",
    }
    assert "security" not in spec["paths"]["/v1/health"]["get"]
    assert {"HTTPBearer": []} in spec["paths"]["/v1/alerts"]["get"]["security"]


@pytest.mark.parametrize(
    "headers",
    [
        with_key("txrisk_0000000000000000_not-a-real-key"),
        with_key("not even shaped like a key"),
        signed_in("not.a.jwt"),
        {"Authorization": "Basic ZGVtbzpkZW1v"},
    ],
    ids=["unknown key", "malformed key", "malformed token", "basic auth"],
)
def test_an_invalid_credential_is_refused(client: TestClient, headers: dict[str, str]):
    assert client.get("/v1/alerts", headers=headers).status_code == 401


def test_a_refused_api_key_is_not_challenged_as_a_bearer_token(client: TestClient):
    response = client.get("/v1/alerts", headers=with_key("txrisk_0000000000000000_nope"))

    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers
    assert response.json() == {"detail": "invalid, expired or revoked API key"}


def test_only_allowed_clients_tokens_are_accepted(database: str, keycloak: Keycloak):
    """Production allows the workbench alone: a token the demo command-line client got with
    the direct grant is refused, though it carries the API's audience and a valid role."""
    access = demo_access(keycloak.realm_url, "analyst", sign_in=keycloak.sign_ins["analyst"])

    with TestClient(create_app(settings(database, oidc_issuer=keycloak.realm_url))) as client:
        response = client.get("/v1/alerts", headers=signed_in(access))

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid bearer token"}


@pytest.mark.parametrize("role", ["analyst", "supervisor", "admin"])
def test_a_demo_user_cannot_sign_in_with_the_realm_placeholder(keycloak: Keycloak, role: str):
    """The committed realm names each credential as a placeholder; it is never the value."""
    with pytest.raises(urllib.error.HTTPError) as refused:
        demo_access(keycloak.realm_url, role, sign_in="${TXRISK_DEMO_SIGNIN_" + role.upper() + "}")

    assert refused.value.code == 401


def test_a_token_and_a_key_together_are_refused(
    client: TestClient, database: str, keycloak: Keycloak
):
    access = demo_access(keycloak.realm_url, "analyst", sign_in=keycloak.sign_ins["analyst"])
    headers = {**signed_in(access), **with_key(issue_key(database))}

    response = client.get("/v1/alerts", headers=headers)

    assert response.status_code == 401
    assert response.json()["detail"] == "send a bearer token or an API key, not both"


def test_without_keycloak_configured_only_api_keys_are_accepted(database: str, keycloak: Keycloak):
    access = demo_access(keycloak.realm_url, "analyst", sign_in=keycloak.sign_ins["analyst"])

    with TestClient(create_app(settings(database))) as client:
        by_bearer = client.get("/v1/alerts", headers=signed_in(access))
        by_key = client.get("/v1/alerts", headers=with_key(issue_key(database, "alerts:read")))

    assert by_bearer.status_code == 401
    assert by_key.status_code == 200
