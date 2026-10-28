"""A stand-in identity provider: an RSA signing key, its JWKS served over HTTP, and access tokens
shaped like Keycloak's, for the token cases real Keycloak will not produce (expired, forged, for
another audience)."""

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

AUDIENCE = "txrisk-api"


def new_signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class LocalIdentityProvider:
    def __init__(self, base_url: str) -> None:
        self.issuer = f"{base_url}/realms/txrisk"
        self.jwks_url = f"{base_url}/certs"
        self.signing_key = new_signing_key()
        self.key_id = "local-key"
        self.jwks: dict[str, Any] = {"keys": [self._jwk(self.signing_key, self.key_id)]}
        # How many times its JWKS has been asked for.
        self.fetches = 0

    @staticmethod
    def _jwk(signing_key: rsa.RSAPrivateKey, key_id: str) -> dict[str, Any]:
        jwk: dict[str, Any] = json.loads(
            jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key())
        )
        return {**jwk, "kid": key_id, "use": "sig", "alg": "RS256"}

    def claims(self, *roles: str, **overrides: Any) -> dict[str, Any]:
        """Claims as Keycloak puts them in an access token for a user with `roles`."""
        now = int(time.time())
        return {
            "iss": self.issuer,
            "aud": AUDIENCE,
            "sub": str(uuid.uuid4()),
            "typ": "Bearer",
            # The client the token was issued to; the workbench is the one production allows.
            "azp": "txrisk-workbench",
            "iat": now,
            "exp": now + 300,
            "preferred_username": f"someone-{'-'.join(roles) or 'without-roles'}",
            "realm_access": {"roles": list(roles)},
            **overrides,
        }

    def sign(
        self,
        claims: dict[str, Any],
        *,
        signing_key: rsa.RSAPrivateKey | None = None,
        key_id: str | None = None,
    ) -> str:
        return jwt.encode(
            claims,
            signing_key or self.signing_key,
            algorithm="RS256",
            headers={"kid": key_id or self.key_id},
        )

    def access(self, *roles: str, **overrides: Any) -> str:
        """A valid access token for a user with `roles`, unless `overrides` spoil it."""
        return self.sign(self.claims(*roles, **overrides))


@contextmanager
def serve_identity_provider(
    *, failing_after: float | None = None
) -> Iterator[LocalIdentityProvider]:
    """The provider, serving its JWKS until the block ends; with `failing_after`, its JWKS
    endpoint instead answers 500 after that many seconds, like a struggling Keycloak."""
    provider: LocalIdentityProvider | None = None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            assert provider is not None
            if self.path != "/certs":
                self.send_error(404)
                return
            provider.fetches += 1
            if failing_after is not None:
                time.sleep(failing_after)
                self.send_error(500)
                return
            payload = json.dumps(provider.jwks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    provider = LocalIdentityProvider(f"http://127.0.0.1:{server.server_address[1]}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield provider
    finally:
        server.shutdown()
        server.server_close()
