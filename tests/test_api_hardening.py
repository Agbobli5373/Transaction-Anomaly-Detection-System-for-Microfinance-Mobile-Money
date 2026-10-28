"""Resisting misuse: per-key rate limits, request size limits, strict CORS and security headers."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from api_helpers import WallClock, body, issue_key, settings, with_key
from fastapi.testclient import TestClient
from pydantic import ValidationError
from scoring_helpers import NOW, tx

from txrisk.api import ApiSettings, create_app

WORKBENCH = "https://workbench.example"


@pytest.fixture
def wall_clock() -> WallClock:
    return WallClock()


@pytest.fixture
def client(database: str, wall_clock: WallClock) -> Iterator[TestClient]:
    app = create_app(
        settings(database, cors_origins=(WORKBENCH,), max_request_bytes=4096),
        wall_clock=wall_clock,
    )
    with TestClient(app) as client:
        yield client


def test_a_key_over_its_rate_limit_gets_429_until_its_allowance_refills(
    client: TestClient, database: str, wall_clock: WallClock
):
    limited = with_key(issue_key(database, "alerts:read", rate_limit_per_minute=3))
    other = with_key(issue_key(database, "alerts:read", rate_limit_per_minute=3))

    statuses = [client.get("/v1/alerts", headers=limited).status_code for _ in range(4)]
    refused = client.get("/v1/alerts", headers=limited)

    assert statuses == [200, 200, 200, 429]
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == "20"
    assert refused.json() == {"detail": "rate limit of 3 requests a minute exceeded"}
    assert client.get("/v1/alerts", headers=other).status_code == 200
    wall_clock.advance(timedelta(seconds=20))
    assert client.get("/v1/alerts", headers=limited).status_code == 200
    assert client.get("/v1/alerts", headers=limited).status_code == 429


def test_denied_requests_count_against_the_limit_too(client: TestClient, database: str):
    """A key probing what it may not do is throttled like any other."""
    headers = with_key(issue_key(database, "alerts:read", rate_limit_per_minute=2))

    statuses = [client.get("/v1/models/active", headers=headers).status_code for _ in range(3)]

    assert statuses == [403, 403, 429]


def test_an_oversized_body_is_rejected_before_anything_reads_it(client: TestClient, database: str):
    headers = with_key(issue_key(database))
    oversized = {**body(replace(tx(NOW), channel="app")), "device_id": "D" * 5000}

    response = client.post("/v1/score", json=oversized, headers=headers)

    assert response.status_code == 413
    assert response.json() == {"detail": "request body over 4096 bytes"}


def test_an_oversized_body_without_a_length_is_cut_off_as_it_streams(
    client: TestClient, database: str
):
    headers = {**with_key(issue_key(database)), "Content-Type": "application/json"}

    def chunks() -> Iterator[bytes]:
        yield b'{"padding": "'
        for _ in range(10):
            yield b"x" * 1000
        yield b'"}'

    response = client.post("/v1/score", content=chunks(), headers=headers)

    assert response.status_code == 413


def test_a_body_within_the_limit_is_scored(client: TestClient, database: str):
    headers = with_key(issue_key(database, "score:write"))

    response = client.post("/v1/score", json=body(replace(tx(NOW), channel="app")), headers=headers)

    # Scored up to the check that its Wallet is registered, which in this empty database it is not.
    assert response.status_code == 422
    assert "w-subject" in response.json()["detail"]


def test_cors_allows_only_the_configured_origins(client: TestClient):
    def preflight(origin: str) -> dict[str, str]:
        response = client.options(
            "/v1/alerts",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        return dict(response.headers)

    allowed = preflight(WORKBENCH)
    refused = preflight("https://evil.example")
    simple = client.get("/v1/health", headers={"Origin": "https://evil.example"})

    assert allowed["access-control-allow-origin"] == WORKBENCH
    assert "authorization" in allowed["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in allowed
    assert "access-control-allow-origin" not in refused
    assert "access-control-allow-origin" not in simple.headers


def test_without_configured_origins_no_browser_origin_is_allowed(database: str):
    with TestClient(create_app(settings(database))) as client:
        response = client.get("/v1/health", headers={"Origin": WORKBENCH})

    assert "access-control-allow-origin" not in response.headers


def test_a_wildcard_origin_is_refused():
    with pytest.raises(ValidationError, match="wildcard"):
        ApiSettings.from_env(
            {"TXRISK_DATABASE_URL": "postgresql://x/y", "TXRISK_CORS_ORIGINS": "*"}
        )


def test_settings_read_the_hardening_from_the_environment():
    loaded = ApiSettings.from_env(
        {
            "TXRISK_DATABASE_URL": "postgresql://x/y",
            "TXRISK_CORS_ORIGINS": f"{WORKBENCH}, http://localhost:3000",
            "TXRISK_MAX_REQUEST_BYTES": "1024",
            "TXRISK_OIDC_ISSUER": "http://localhost:8080/realms/txrisk",
        }
    )

    assert loaded.cors_origins == (WORKBENCH, "http://localhost:3000")
    assert loaded.max_request_bytes == 1024
    assert loaded.jwks_url == "http://localhost:8080/realms/txrisk/protocol/openid-connect/certs"


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/v1/health", 200),
        ("GET", "/v1/alerts", 401),
        ("GET", "/v1/nowhere", 404),
        ("POST", "/v1/score", 413),
    ],
)
def test_every_response_carries_the_security_headers(
    client: TestClient, method: str, path: str, status: int
):
    response = client.request(method, path, content=b"x" * 5000 if status == 413 else None)

    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["strict-transport-security"].startswith("max-age=")
    assert response.headers["cache-control"] == "no-store"
    assert (
        response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    )


def test_the_interactive_docs_may_load_swagger_ui_and_nothing_else(client: TestClient):
    response = client.get("/v1/docs")

    csp = response.headers["content-security-policy"]
    assert response.status_code == 200
    assert "https://cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp
