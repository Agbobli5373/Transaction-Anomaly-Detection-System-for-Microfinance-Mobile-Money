"""Bearer tokens Keycloak itself would never issue: expired, forged, for another API or realm.

These are signed by a stand-in identity provider whose JWKS the API fetches over HTTP, just as it
fetches Keycloak's; test_api_access.py covers the tokens real Keycloak issues.
"""

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from api_helpers import settings, signed_in
from fastapi.testclient import TestClient
from idp_helpers import LocalIdentityProvider, new_signing_key

from txrisk.api import create_app


@pytest.fixture
def client(database: str, local_idp: LocalIdentityProvider) -> Iterator[TestClient]:
    app = create_app(
        settings(database, oidc_issuer=local_idp.issuer, oidc_jwks_url=local_idp.jwks_url)
    )
    with TestClient(app) as client:
        yield client


def test_a_valid_token_for_a_role_is_accepted(client: TestClient, local_idp: LocalIdentityProvider):
    response = client.get("/v1/alerts", headers=signed_in(local_idp.access("analyst")))

    assert response.status_code == 200


@pytest.mark.parametrize(
    "spoilt",
    [
        {"exp": int(time.time()) - 60, "iat": int(time.time()) - 400},
        {"aud": "some-other-api"},
        {"iss": "http://keycloak.example/realms/txrisk"},
        {"typ": "ID"},
        {"exp": None},
    ],
    ids=["expired", "another audience", "another issuer", "an id token", "no expiry"],
)
def test_a_token_not_meant_for_this_api_now_is_refused(
    client: TestClient, local_idp: LocalIdentityProvider, spoilt: dict[str, Any]
):
    claims = {k: v for k, v in local_idp.claims("analyst", **spoilt).items() if v is not None}

    response = client.get("/v1/alerts", headers=signed_in(local_idp.sign(claims)))

    assert response.status_code == 401
    assert "invalid_token" in response.headers["WWW-Authenticate"]


def test_a_token_signed_with_another_key_under_the_realms_key_id_is_refused(
    client: TestClient, local_idp: LocalIdentityProvider
):
    forged = local_idp.sign(local_idp.claims("supervisor"), signing_key=new_signing_key())

    assert client.get("/v1/alerts", headers=signed_in(forged)).status_code == 401


def test_a_token_naming_an_unknown_key_is_refused(
    client: TestClient, local_idp: LocalIdentityProvider
):
    stranger = local_idp.sign(local_idp.claims("analyst"), key_id="someone-elses-key")

    assert client.get("/v1/alerts", headers=signed_in(stranger)).status_code == 401


def test_unsigned_and_symmetric_tokens_are_refused(
    client: TestClient, local_idp: LocalIdentityProvider
):
    claims = local_idp.claims("admin")
    unsigned = jwt.encode(claims, None, algorithm="none", headers={"kid": local_idp.key_id})
    # The classic confusion: an HMAC "signed" with the realm's public key, which anyone has.
    public = local_idp.jwks["keys"][0]["n"]
    hmac_signed = jwt.encode(claims, public, algorithm="HS256", headers={"kid": local_idp.key_id})

    for presented in (unsigned, hmac_signed):
        response = client.get("/v1/models/active", headers=signed_in(presented))
        assert response.status_code == 401


def test_a_person_without_a_known_role_is_authenticated_but_permitted_nothing(
    client: TestClient, local_idp: LocalIdentityProvider
):
    for roles in ((), ("offline_access", "uma_authorization")):
        headers = signed_in(local_idp.access(*roles))
        assert client.get("/v1/models/active", headers=headers).status_code == 403
        assert client.get("/v1/alerts", headers=headers).status_code == 403


def test_unknown_roles_alongside_a_known_one_are_ignored(
    client: TestClient, local_idp: LocalIdentityProvider
):
    headers = signed_in(local_idp.access("analyst", "default-roles-txrisk"))

    assert client.get("/v1/alerts", headers=headers).status_code == 200
    assert client.get("/v1/api-keys", headers=headers).status_code == 403


def test_tokens_cannot_be_checked_while_keycloak_is_unreachable(
    database: str, local_idp: LocalIdentityProvider
):
    app = create_app(
        settings(
            database,
            oidc_issuer=local_idp.issuer,
            oidc_jwks_url="http://127.0.0.1:1/realms/txrisk/protocol/openid-connect/certs",
        )
    )
    with TestClient(app) as client:
        response = client.get("/v1/alerts", headers=signed_in(local_idp.access("analyst")))
        health = client.get("/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "the identity provider is unavailable"}
    assert health.status_code == 200
