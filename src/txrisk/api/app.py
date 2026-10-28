"""The HTTP API, under `/v1`, with its OpenAPI spec at `/v1/openapi.json`.

Every `/v1` endpoint needs a person's Keycloak access token with the right role, or a service's API
key with the right scope (see `txrisk.api.auth`). Liveness, readiness, the OpenAPI spec and the
interactive docs stay open: they reveal no data, and probes carry no credentials.

The API never changes the schema: `txrisk migrate` does that as its own job, and readiness
reports unready until it has.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from txrisk.api.auth import (
    API_KEY_HEADER,
    MANAGE_API_KEYS,
    READ_ALERTS,
    READ_MODELS,
    READ_TRANSACTIONS,
    SCORE,
    Authenticator,
    Principal,
    TokenVerifier,
    require,
)
from txrisk.api.hardening import RateLimiter, harden
from txrisk.api.schemas import (
    ActiveModel,
    AlertOut,
    AlertPage,
    ApiKeyIn,
    ApiKeyOut,
    AssessmentOut,
    IssuedApiKeyOut,
    RotationIn,
    Status,
    TransactionIn,
    TransactionOut,
    TransactionView,
)
from txrisk.api.settings import ApiSettings
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import RULES_ONLY
from txrisk.store import (
    StoredScoring,
    TransactionConflict,
    UnknownParty,
    api_keys,
    create_engine,
    pending_migrations,
    records,
)

# Set on a scoring response that returns the assessment an earlier call stored.
REPLAYED_HEADER = "Idempotent-Replayed"

_DENIED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No credential, or an invalid, expired or revoked one"},
    403: {"description": "The caller's roles or scopes do not permit this"},
    429: {"description": "The API key's rate limit is exceeded; see Retry-After"},
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(settings: ApiSettings, *, wall_clock: Callable[[], datetime] = _utc_now) -> FastAPI:
    """The API over the database and model bundle in `settings`.

    The bundle is loaded once, here; a configured bundle that cannot be loaded stops the API from
    starting rather than letting it score with something other than what was configured.
    `wall_clock` is real time, used only for API key expiry and rate limits; scoring runs on each
    Transaction's event time (ADR 0005).
    """
    engine = create_engine(settings.database_url)
    bundle = None if settings.model_bundle is None else ModelBundle.load(settings.model_bundle)
    rules = settings.rules()
    scoring = StoredScoring(
        engine,
        features=settings.features(),
        rules=rules,
        bundle=bundle,
        cache=settings.history_cache(),
    )
    if bundle is None:
        active = ActiveModel(
            model_version=RULES_ONLY,
            thresholds={"medium": rules.bands.medium, "high": rules.bands.high},
            fitted_on=None,
        )
    else:
        active = ActiveModel(
            model_version=bundle.version,
            thresholds={"medium": bundle.thresholds.medium, "high": bundle.thresholds.high},
            fitted_on=bundle.fitted_on,
        )
    jwks_url = settings.jwks_url
    verifier = (
        None
        if settings.oidc_issuer is None or jwks_url is None
        else TokenVerifier(
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            jwks_url=jwks_url,
            allowed_clients=settings.oidc_allowed_clients,
        )
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Transaction risk scoring",
        version="1",
        description=(
            "Scores mobile money Transactions for risk and explains why. Advisory only: it "
            "raises Alerts for people to review and never blocks a Transaction.\n\n"
            "People call with a Keycloak access token (`Authorization: Bearer ...`); services "
            f"with a scoped API key (`{API_KEY_HEADER}: ...`). Health and readiness are open."
        ),
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.authenticator = Authenticator(
        engine, verifier=verifier, limiter=RateLimiter(wall_clock), wall_clock=wall_clock
    )
    harden(
        app,
        cors_origins=settings.cors_origins,
        allowed_headers=["Authorization", "Content-Type", API_KEY_HEADER],
        exposed_headers=[REPLAYED_HEADER, "Retry-After"],
        max_request_bytes=settings.max_request_bytes,
    )
    v1 = APIRouter(prefix="/v1")

    @v1.post(
        "/score",
        response_model=AssessmentOut,
        tags=["scoring"],
        dependencies=[Depends(require(SCORE))],
        responses=_DENIED,
    )
    def score(transaction: TransactionIn, response: Response) -> AssessmentOut:
        """Score a Transaction and store it with its Risk assessment; a High Risk band raises
        an Alert. Idempotent on `transaction_id`: a repeat returns the stored assessment, marked
        with the `Idempotent-Replayed: true` header, and raises no second Alert.
        Needs the `score:write` scope."""
        try:
            scored = scoring.assess(transaction.to_transaction())
        except UnknownParty as e:
            raise HTTPException(422, f"not registered: {e}") from e
        except TransactionConflict as e:
            raise HTTPException(409, f"a different Transaction is already stored as {e}") from e
        if scored.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return AssessmentOut.of(scored.assessment)

    @v1.get(
        "/transactions/{transaction_id}",
        response_model=TransactionView,
        tags=["transactions"],
        dependencies=[Depends(require(READ_TRANSACTIONS))],
        responses={**_DENIED, 404: {"description": "No such Transaction"}},
    )
    def get_transaction(transaction_id: str) -> TransactionView:
        """A stored Transaction and its Risk assessment, if it was scored. For Analysts and
        Supervisors, or the `transactions:read` scope."""
        with engine.connect() as connection:
            transaction = records.stored_transaction(connection, transaction_id)
            if transaction is None:
                raise HTTPException(404, "no such Transaction")
            assessment = records.stored_assessment(connection, transaction_id)
        return TransactionView(
            transaction=TransactionOut.of(transaction),
            assessment=None if assessment is None else AssessmentOut.of(assessment),
        )

    @v1.get(
        "/alerts",
        response_model=AlertPage,
        tags=["alerts"],
        dependencies=[Depends(require(READ_ALERTS))],
        responses=_DENIED,
    )
    def get_alerts(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        before: Annotated[int | None, Query(description="next_before of the previous page")] = None,
        subject_wallet_id: str | None = None,
    ) -> AlertPage:
        """Alerts, newest first. For Analysts and Supervisors, or the `alerts:read` scope."""
        with engine.connect() as connection:
            found = records.list_alerts(
                connection, limit=limit, before_id=before, subject_wallet_id=subject_wallet_id
            )
        return AlertPage(
            alerts=[
                AlertOut(
                    alert_id=a.alert_id,
                    transaction_id=a.transaction_id,
                    subject_wallet_id=a.subject_wallet_id,
                    raised_at=a.raised_at,
                    assessment=AssessmentOut.of(a.assessment),
                )
                for a in found
            ],
            next_before=found[-1].alert_id if len(found) == limit else None,
        )

    @v1.get(
        "/models/active",
        response_model=ActiveModel,
        tags=["models"],
        dependencies=[Depends(require(READ_MODELS))],
        responses=_DENIED,
    )
    def get_active_model() -> ActiveModel:
        """The model version that scores, and its Risk band thresholds. For every role, or the
        `models:read` scope."""
        return active

    _add_api_key_routes(v1, engine, settings, wall_clock)

    @v1.get("/health", response_model=Status, tags=["operations"])
    def health() -> Status:
        """Liveness: the process is up. It does not touch the database. Open to all."""
        return Status(status="ok")

    @v1.get(
        "/ready",
        response_model=Status,
        tags=["operations"],
        responses={503: {"model": Status, "description": "Not ready to score"}},
    )
    def ready() -> Status | JSONResponse:
        """Readiness: the database answers and every migration has been applied. Open to all."""
        try:
            with engine.connect() as connection:
                pending = pending_migrations(connection)
        except sa.exc.SQLAlchemyError:
            return _unavailable("database unreachable")
        if pending:
            return _unavailable(f"migrations not applied: {', '.join(pending)}")
        return Status(status="ok")

    app.include_router(v1)
    return app


def _add_api_key_routes(
    v1: APIRouter, engine: sa.Engine, settings: ApiSettings, wall_clock: Callable[[], datetime]
) -> None:
    """Issuing, listing, rotating and revoking API keys: for Admins signed in, never for a key."""
    admin = Annotated[Principal, Depends(require(MANAGE_API_KEYS))]
    unknown: dict[int | str, dict[str, Any]] = {404: {"description": "No such API key"}}

    @v1.post(
        "/api-keys",
        response_model=IssuedApiKeyOut,
        status_code=201,
        tags=["api keys"],
        responses=_DENIED,
    )
    def issue_api_key(wanted: ApiKeyIn, principal: admin) -> IssuedApiKeyOut:
        """Issue a key for a service. The key is in this response only: just its hash is
        stored."""
        now = wall_clock()
        with engine.begin() as connection:
            issued = api_keys.issue(
                connection,
                name=wanted.name,
                scopes=wanted.scopes,
                rate_limit_per_minute=wanted.rate_limit_per_minute or settings.api_key_rate_limit,
                created_by=principal.name,
                now=now,
                expires_at=wanted.expires_at,
            )
        return IssuedApiKeyOut(key=issued.key, api_key=ApiKeyOut.of(issued.record, now))

    @v1.get("/api-keys", response_model=list[ApiKeyOut], tags=["api keys"], responses=_DENIED)
    def list_api_keys(_: admin) -> list[ApiKeyOut]:
        """Every key ever issued, revoked and expired ones included, oldest first. Never the
        keys themselves."""
        now = wall_clock()
        with engine.connect() as connection:
            return [ApiKeyOut.of(record, now) for record in api_keys.list_keys(connection)]

    @v1.post(
        "/api-keys/{key_id}/rotate",
        response_model=IssuedApiKeyOut,
        status_code=201,
        tags=["api keys"],
        responses={
            **_DENIED,
            **unknown,
            409: {"description": "The key is revoked, expired or already rotated"},
        },
    )
    def rotate_api_key(
        key_id: str, principal: admin, rotation: Annotated[RotationIn | None, Body()] = None
    ) -> IssuedApiKeyOut:
        """Issue a replacement with the same name, scopes, rate limit and expiry. The old key
        keeps working for the overlap (a day by default), so the service switches without
        downtime. A key is rotated once; rotate its replacement next time."""
        minutes = None if rotation is None else rotation.overlap_minutes
        if minutes is None:
            minutes = settings.api_key_rotation_overlap_minutes
        now = wall_clock()
        try:
            with engine.begin() as connection:
                issued = api_keys.rotate(
                    connection,
                    key_id,
                    overlap=timedelta(minutes=minutes),
                    created_by=principal.name,
                    now=now,
                )
        except api_keys.UnknownApiKey as e:
            raise HTTPException(404, "no such API key") from e
        except api_keys.InactiveApiKey as e:
            raise HTTPException(409, "the API key is revoked or expired; issue a new one") from e
        except api_keys.AlreadyRotated as e:
            raise HTTPException(
                409, "the API key has already been rotated; rotate its replacement"
            ) from e
        return IssuedApiKeyOut(key=issued.key, api_key=ApiKeyOut.of(issued.record, now))

    @v1.delete(
        "/api-keys/{key_id}",
        response_model=ApiKeyOut,
        tags=["api keys"],
        responses={**_DENIED, **unknown},
    )
    def revoke_api_key(key_id: str, _: admin) -> ApiKeyOut:
        """Revoke a key at once, as after a leak."""
        now = wall_clock()
        try:
            with engine.begin() as connection:
                record = api_keys.revoke(connection, key_id, now=now)
        except api_keys.UnknownApiKey as e:
            raise HTTPException(404, "no such API key") from e
        return ApiKeyOut.of(record, now)


def _unavailable(detail: str) -> JSONResponse:
    return JSONResponse(Status(status="unavailable", detail=detail).model_dump(), status_code=503)


def app_from_env() -> FastAPI:
    """The API configured from `TXRISK_*` environment variables, for `uvicorn --factory`."""
    return create_app(ApiSettings.from_env())
