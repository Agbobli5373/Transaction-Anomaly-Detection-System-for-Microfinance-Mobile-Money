"""Who is calling, and what they may do: the authorisation layer every `/v1` endpoint uses.

People sign in through Keycloak (OIDC) and call with its access token as a bearer token; their
realm roles are Analyst, Supervisor and Admin. Services call with a scoped API key in the
`X-API-Key` header. Either way the caller becomes a `Principal`, and each endpoint names the one
`Permission` it needs:

    @router.get("/alerts")
    def alerts(principal: Annotated[Principal, Depends(require(READ_ALERTS))]) -> ...

A Permission lists the roles that hold it and the API key scope that grants it, so one check
covers both kinds of caller. Endpoints added later (the audit log, Cases, model Promotion) declare
their own Permissions the same way and add them to `PERMISSIONS`. The authenticated Principal is
also left on `request.state.principal`, for anything that records who did what.
"""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

import jwt
import sqlalchemy as sa
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from txrisk.api.audit import AuditedRoute, Auditor
from txrisk.api.hardening import RateLimiter, retry_after
from txrisk.store import api_keys

API_KEY_HEADER = "X-API-Key"

_log = logging.getLogger(__name__)


class Role(StrEnum):
    """A person's role, as a Keycloak realm role."""

    ANALYST = "analyst"
    SUPERVISOR = "supervisor"
    ADMIN = "admin"


@dataclass(frozen=True)
class Permission:
    # What it allows, as it reads in "not permitted to ...".
    action: str
    # The roles whose holders may do it.
    roles: frozenset[Role]
    # The API key scope that grants it to a service; None for what only people may do.
    scope: str | None


_ROLE_NAMES = frozenset(r.value for r in Role)
PEOPLE_WHO_INVESTIGATE = frozenset({Role.ANALYST, Role.SUPERVISOR})

# Scoring is a service's job: people read results, they never submit Transactions.
SCORE = Permission("score Transactions", frozenset(), "score:write")
READ_TRANSACTIONS = Permission(
    "read Transactions and their Risk assessments", PEOPLE_WHO_INVESTIGATE, "transactions:read"
)
READ_ALERTS = Permission("read Alerts", PEOPLE_WHO_INVESTIGATE, "alerts:read")
READ_MODELS = Permission("read the active model", frozenset(Role), "models:read")
# No scope: an API key can never issue another, so a leaked key cannot mint more.
MANAGE_API_KEYS = Permission("issue, rotate and revoke API keys", frozenset({Role.ADMIN}), None)

# Promotion and rollback change what scores every Transaction: Admins alone, signed in, so a
# leaked key cannot swap the model. The gates are checked server-side whoever asks.
PROMOTE_MODELS = Permission(
    "promote and roll back models, and run their gates", frozenset({Role.ADMIN}), None
)

# People only, and Admins only: the audit log is read by whoever is accountable for access.
READ_AUDIT = Permission("read the audit log", frozenset({Role.ADMIN}), None)

# Cases and the watch list are for the people who work them: no API key scope reaches them.
READ_CASES = Permission("read Cases", PEOPLE_WHO_INVESTIGATE, None)
ASSIGN_CASES = Permission("assign Cases to Analysts", frozenset({Role.SUPERVISOR}), None)
# Further limited, in the Case itself, to the assigned Analyst or a Supervisor.
WORK_CASES = Permission("add notes to and move Cases", PEOPLE_WHO_INVESTIGATE, None)
READ_WATCHLIST = Permission("read the Medium watch list", PEOPLE_WHO_INVESTIGATE, None)
# A report is drafted with WORK_CASES (the assigned Analyst or a Supervisor). Approving and
# exporting it are a Supervisor's alone: no Analyst, Admin or API key.
APPROVE_REPORTS = Permission(
    "approve Suspicious transaction reports", frozenset({Role.SUPERVISOR}), None
)
EXPORT_REPORTS = Permission(
    "export Suspicious transaction reports", frozenset({Role.SUPERVISOR}), None
)

PERMISSIONS = (
    SCORE,
    READ_TRANSACTIONS,
    READ_ALERTS,
    READ_MODELS,
    MANAGE_API_KEYS,
    PROMOTE_MODELS,
    READ_AUDIT,
    READ_CASES,
    ASSIGN_CASES,
    WORK_CASES,
    READ_WATCHLIST,
    APPROVE_REPORTS,
    EXPORT_REPORTS,
)
# Every scope an API key may carry.
SCOPES = tuple(sorted(p.scope for p in PERMISSIONS if p.scope is not None))


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: a person signed in through Keycloak, or a service's API key."""

    kind: Literal["person", "service"]
    # Keycloak's subject ID for a person; the key ID for a service.
    subject: str
    # The person's username, or the API key's name.
    name: str
    roles: frozenset[Role] = frozenset()
    scopes: frozenset[str] = frozenset()

    def can(self, permission: Permission) -> bool:
        if self.kind == "person":
            return bool(self.roles & permission.roles)
        return permission.scope is not None and permission.scope in self.scopes


class _Unauthenticated(HTTPException):
    """A 401. Bearer-token failures carry the Bearer challenge; an API key has no standard
    scheme to challenge with, so its failures carry none."""

    def __init__(self, detail: str, *, challenge: str | None = 'Bearer realm="txrisk"') -> None:
        headers = None if challenge is None else {"WWW-Authenticate": challenge}
        super().__init__(401, detail, headers=headers)


# One message for every bad bearer token, so a caller cannot tell which check failed; the server
# logs the reason.
_REFUSED_BEARER = "invalid bearer token"
_REFUSED_BEARER_CHALLENGE = 'Bearer realm="txrisk", error="invalid_token"'


def _invalid_token(reason: str) -> _Unauthenticated:
    _log.info("refused a bearer token: %s", reason)
    return _Unauthenticated(_REFUSED_BEARER, challenge=_REFUSED_BEARER_CHALLENGE)


def _signing_keys(jwks: Mapping[str, Any]) -> dict[str, jwt.PyJWK]:
    """The JWKS's signing keys by key ID; Keycloak also publishes an encryption key."""
    found = {}
    for entry in jwks.get("keys", []):
        if entry.get("use", "sig") != "sig" or "kid" not in entry:
            continue
        try:
            found[entry["kid"]] = jwt.PyJWK.from_dict(entry)
        except jwt.PyJWKError:
            continue  # an algorithm this library cannot use
    return found


class TokenVerifier:
    """Checks Keycloak access tokens against the realm's published signing keys.

    The keys are fetched on first use, not at startup, so the API starts and scores for services
    while Keycloak is still coming up. They are fetched again when a token names a key ID not
    seen before (Keycloak rotated its keys), at most once every `refresh_interval`, whether the
    last fetch worked or failed: a flood of tokens naming made-up key IDs, or a slow Keycloak,
    costs one short fetch per interval, not one per request. Fetches are one at a time; requests
    arriving meanwhile wait for its result rather than fetching again.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str,
        allowed_clients: Collection[str],
        algorithms: tuple[str, ...] = ("RS256",),
        leeway: timedelta = timedelta(seconds=10),
        refresh_interval: timedelta = timedelta(seconds=10),
        fetch_timeout: timedelta = timedelta(seconds=2),
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self.allowed_clients = frozenset(allowed_clients)
        self._algorithms = list(algorithms)
        self._leeway = leeway
        self._refresh_interval = refresh_interval.total_seconds()
        self._fetch_timeout = fetch_timeout.total_seconds()
        self._keys: dict[str, jwt.PyJWK] = {}
        # When the last fetch was tried, and whether it failed.
        self._fetched_at: float | None = None
        self._fetch_failed = False
        self._lock = threading.Lock()

    def _fetch(self) -> None:
        try:
            with urllib.request.urlopen(self.jwks_url, timeout=self._fetch_timeout) as response:
                keys = _signing_keys(json.load(response))
        except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
            _log.warning("could not fetch the realm's signing keys: %s", e)
            self._fetch_failed = True
        else:
            self._keys = keys
            self._fetch_failed = False
        finally:
            self._fetched_at = time.monotonic()

    def _key(self, key_id: str) -> jwt.PyJWK | None:
        # Known keys never wait behind a fetch in progress.
        known = self._keys.get(key_id)
        if known is not None:
            return known
        with self._lock:
            if key_id not in self._keys:
                due = (
                    self._fetched_at is None
                    or time.monotonic() - self._fetched_at >= self._refresh_interval
                )
                if due:
                    self._fetch()
                if key_id not in self._keys and self._fetch_failed:
                    raise HTTPException(503, "the identity provider is unavailable")
            return self._keys.get(key_id)

    def verify(self, presented: str) -> Principal:
        """The person the access token was issued to. Raises a 401 for anything but a current,
        correctly signed access token from this realm, for this API, from an allowed client."""
        try:
            header = jwt.get_unverified_header(presented)
        except jwt.InvalidTokenError as e:
            raise _invalid_token(f"malformed: {e}") from e
        key_id = str(header.get("kid", ""))
        key = self._key(key_id)
        if key is None:
            raise _invalid_token(f"signed with unknown key {key_id!r}")
        try:
            claims = jwt.decode(
                presented,
                key,
                algorithms=self._algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.InvalidTokenError as e:
            raise _invalid_token(str(e)) from e
        # Keycloak marks access tokens "Bearer"; ID and refresh tokens are not credentials here.
        if claims.get("typ", "Bearer") != "Bearer":
            raise _invalid_token(f"not an access token: typ {claims.get('typ')!r}")
        # Every client in the realm may add this API's audience; only the allowed ones may call.
        if claims.get("azp") not in self.allowed_clients:
            raise _invalid_token(f"issued to client {claims.get('azp')!r}, which is not allowed")
        realm_access = claims.get("realm_access")
        granted = realm_access.get("roles", []) if isinstance(realm_access, dict) else []
        return Principal(
            kind="person",
            subject=str(claims["sub"]),
            name=str(claims.get("preferred_username", claims["sub"])),
            # Keycloak's own roles (default-roles-txrisk, offline_access, ...) grant nothing here.
            roles=frozenset(Role(r) for r in granted if isinstance(r, str) and r in _ROLE_NAMES),
        )


@dataclass(frozen=True)
class Authenticator:
    """Turns a request's credential into a Principal, applying each API key's rate limit."""

    engine: sa.Engine
    # None when no identity provider is configured: then only API keys are accepted.
    verifier: TokenVerifier | None
    limiter: RateLimiter
    # The real time, for API key expiry: keys expire in wall-clock time, unlike scoring's
    # event-time windows (ADR 0005).
    wall_clock: Callable[[], datetime]

    def principal(self, *, bearer: str | None, api_key: str | None) -> Principal:
        if bearer is not None and api_key is not None:
            raise _Unauthenticated("send a bearer token or an API key, not both")
        if bearer is not None:
            if self.verifier is None:
                raise _Unauthenticated("sign-in is not configured; use an API key")
            return self.verifier.verify(bearer)
        if api_key is not None:
            return self._service(api_key)
        raise _Unauthenticated(f"send a bearer token or an {API_KEY_HEADER} header")

    def _service(self, presented: str) -> Principal:
        with self.engine.connect() as connection:
            record = api_keys.authenticate(connection, presented, self.wall_clock())
        if record is None:
            raise _Unauthenticated("invalid, expired or revoked API key", challenge=None)
        wait = self.limiter.take(record.key_id, record.rate_limit_per_minute)
        if wait is not None:
            raise HTTPException(
                429,
                f"rate limit of {record.rate_limit_per_minute} requests a minute exceeded",
                headers={"Retry-After": retry_after(wait)},
            )
        return Principal(
            kind="service", subject=record.key_id, name=record.name, scopes=record.scopes
        )


_bearer_scheme = HTTPBearer(
    auto_error=False, description="A Keycloak access token for a person signed in to the realm."
)
_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER, auto_error=False, description="A scoped API key, for services."
)


def authenticated(
    request: Request,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    api_key: Annotated[str | None, Depends(_api_key_scheme)],
) -> Principal:
    """The caller, or a 401 (no or bad credential), 429 (rate limit) or 503 (Keycloak down)."""
    authenticator: Authenticator = request.app.state.authenticator
    principal = authenticator.principal(
        bearer=None if bearer is None else bearer.credentials, api_key=api_key
    )
    request.state.principal = principal
    return principal


def require(
    permission: Permission, audited: AuditedRoute | None = None
) -> Callable[[Request, Principal], Principal]:
    """A dependency giving the caller if they hold `permission`, and a 403 if they do not.

    A route that names itself in `audited` writes the refusal to the audit log first, so denied
    attempts on sensitive routes are on record (the endpoint records its own successes).
    """

    def permitted(
        request: Request, principal: Annotated[Principal, Depends(authenticated)]
    ) -> Principal:
        if not principal.can(permission):
            if audited is not None:
                auditor: Auditor = request.app.state.auditor
                auditor.record_alone(
                    principal,
                    audited.action,
                    object_type=audited.object_type,
                    object_id=request.path_params.get(audited.object_param or ""),
                    outcome="denied",
                    detail={"needed": permission.action},
                )
            raise HTTPException(403, f"not permitted to {permission.action}")
        return principal

    return permitted
