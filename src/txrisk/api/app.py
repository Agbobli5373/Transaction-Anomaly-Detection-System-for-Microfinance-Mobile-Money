"""The HTTP API, under `/v1`, with its OpenAPI spec at `/v1/openapi.json`.

Every `/v1` endpoint needs a person's Keycloak access token with the right role, or a service's API
key with the right scope (see `txrisk.api.auth`). Liveness, readiness, the OpenAPI spec and the
interactive docs stay open: they reveal no data, and probes carry no credentials.

The API never changes the schema: `txrisk migrate` does that as its own job, and readiness
reports unready until it has.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Response,
)
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from txrisk.api.audit import AuditedRoute, Auditor, AuditorDep
from txrisk.api.auth import (
    API_KEY_HEADER,
    MANAGE_API_KEYS,
    READ_ALERTS,
    READ_AUDIT,
    READ_MODELS,
    READ_TRANSACTIONS,
    SCORE,
    Authenticator,
    Principal,
    TokenVerifier,
    require,
)
from txrisk.api.cases import add_case_routes
from txrisk.api.hardening import RateLimiter, harden
from txrisk.api.schemas import (
    ActiveModel,
    AlertOut,
    AlertPage,
    ApiKeyIn,
    ApiKeyOut,
    AssessmentOut,
    AuditEntryOut,
    AuditPage,
    IssuedApiKeyOut,
    Readiness,
    RotationIn,
    Status,
    TransactionIn,
    TransactionOut,
    TransactionView,
)
from txrisk.api.settings import ApiSettings
from txrisk.observability import logs
from txrisk.observability.asgi import CORRELATION_HEADER, Observability
from txrisk.observability.metrics import Metrics, stopwatch
from txrisk.scoring import RULES_ONLY
from txrisk.store import (
    SimulationClock,
    StoredScoring,
    TransactionConflict,
    UnknownParty,
    api_keys,
    audit,
    create_app_engine,
    pending_migrations,
    records,
)

log = logging.getLogger(__name__)

# Set on a scoring response that returns the assessment an earlier call stored.
REPLAYED_HEADER = "Idempotent-Replayed"

# Degraded assessments re-scored per background run after recovery; a run that leaves more
# queues another, and `txrisk rescore` takes the rest.
RESCORE_BATCH = 500

_DENIED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No credential, or an invalid, expired or revoked one"},
    403: {"description": "The caller's roles or scopes do not permit this"},
    429: {"description": "The API key's rate limit is exceeded; see Retry-After"},
}


# How each sensitive route is named in the audit log. Scoring is not here: see txrisk.api.audit.
VIEW_TRANSACTION = AuditedRoute("transaction.view", "transaction", "transaction_id")
LIST_ALERTS = AuditedRoute("alert.list", "alert")
ISSUE_API_KEY = AuditedRoute("api_key.issue", "api_key")
LIST_API_KEYS = AuditedRoute("api_key.list", "api_key")
ROTATE_API_KEY = AuditedRoute("api_key.rotate", "api_key", "key_id")
REVOKE_API_KEY = AuditedRoute("api_key.revoke", "api_key", "key_id")
READ_AUDIT_LOG = AuditedRoute("audit.read", "audit_log")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def create_app(settings: ApiSettings, *, wall_clock: Callable[[], datetime] = _utc_now) -> FastAPI:
    """The API over the database and model bundle in `settings`.

    The Champion model bundle is loaded here if it can be, and again whenever it is needed and
    not loaded. One that is missing, corrupt, slow or failing does not stop the API: it scores in
    Degraded mode (Rule hits alone, marked degraded) and reports `degraded` on `/v1/ready`, until
    the model is healthy again, when the degraded assessments are re-scored in the background.
    `wall_clock` is real time, used only for API key expiry, rate limits and the time of audit
    entries; scoring runs on each Transaction's event time (ADR 0005).
    """
    engine = create_app_engine(settings.database_url)
    rules = settings.rules()
    metrics = Metrics()
    scoring = StoredScoring(
        engine,
        features=settings.features(),
        rules=rules,
        bundle=settings.champion(metrics),
        cache=settings.history_cache(),
        metrics=metrics,
    )
    scoring.model_health()  # load the bundle now, if it can be, so the first Transaction is fast
    clock = SimulationClock(engine)
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
        # Metrics are served on a port of their own, which compose never publishes, so they are
        # not under /v1 and need no credential.
        exporter = None if settings.metrics_port is None else metrics.serve(settings.metrics_port)
        try:
            yield
        finally:
            if exporter is not None:
                exporter[0].shutdown()
                exporter[0].server_close()
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
    app.state.auditor = Auditor(engine, wall_clock)
    app.state.authenticator = Authenticator(
        engine, verifier=verifier, limiter=RateLimiter(wall_clock), wall_clock=wall_clock
    )
    harden(
        app,
        cors_origins=settings.cors_origins,
        allowed_headers=["Authorization", "Content-Type", API_KEY_HEADER],
        exposed_headers=[REPLAYED_HEADER, "Retry-After", CORRELATION_HEADER],
        max_request_bytes=settings.max_request_bytes,
    )
    # Outermost, so a request any other layer refuses is still counted, logged and traced.
    app.add_middleware(Observability, metrics=metrics)
    v1 = APIRouter(prefix="/v1")

    @v1.post(
        "/score",
        response_model=AssessmentOut,
        tags=["scoring"],
        dependencies=[Depends(require(SCORE))],
        responses=_DENIED,
    )
    def score(
        transaction: TransactionIn, response: Response, background: BackgroundTasks
    ) -> AssessmentOut:
        """Score a Transaction and store it with its Risk assessment; a High Risk band raises
        an Alert. Idempotent on `transaction_id`: a repeat returns the stored assessment, marked
        with the `Idempotent-Replayed: true` header, and raises no second Alert.

        If the Champion model is unavailable or too slow, the Transaction is scored from Rule
        hits alone: `degraded` is true, `model_version` is `rules-only` and the score and band
        are on the rules scale (see AssessmentOut). It is re-scored once the model is healthy.
        Needs the `score:write` scope."""
        domain = transaction.to_transaction()
        logs.bind_transaction(domain.transaction_id)
        elapsed = stopwatch()
        try:
            scored = scoring.assess(domain)
        except UnknownParty as e:
            metrics.scoring_errors.labels("api", "unknown_party").inc()
            raise HTTPException(422, f"not registered: {e}") from e
        except TransactionConflict as e:
            metrics.scoring_errors.labels("api", "conflict").inc()
            raise HTTPException(409, f"a different Transaction is already stored as {e}") from e
        except Exception:
            metrics.scoring_errors.labels("api", "failure").inc()
            log.exception("scoring failed")
            raise
        metrics.observe_score(
            "api",
            scored.assessment,
            replayed=scored.replayed,
            seconds=elapsed(),
            occurred_at=domain.occurred_at,
        )
        log.info(
            "scored: %s",
            scored.assessment.risk_band.value,
            extra={
                "risk_band": scored.assessment.risk_band.value,
                "degraded": scored.assessment.degraded,
                "replayed": scored.replayed,
            },
        )
        # The simulation clock follows the latest event time seen (ADR 0005).
        clock.advance(transaction.occurred_at)
        if scored.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        if scoring.rescore_due():
            background.add_task(scoring.rescore_degraded, limit=RESCORE_BATCH)
        return AssessmentOut.of(scored.assessment)

    @v1.get(
        "/transactions/{transaction_id}",
        response_model=TransactionView,
        tags=["transactions"],
        responses={**_DENIED, 404: {"description": "No such Transaction"}},
    )
    def get_transaction(
        transaction_id: str,
        principal: Annotated[Principal, Depends(require(READ_TRANSACTIONS, VIEW_TRANSACTION))],
        auditor: AuditorDep,
    ) -> TransactionView:
        """A stored Transaction and its Risk assessment, if it was scored. For Analysts and
        Supervisors, or the `transactions:read` scope. Audited."""
        with engine.begin() as connection:
            transaction = records.stored_transaction(connection, transaction_id)
            assessment = (
                None
                if transaction is None
                else records.stored_assessment(connection, transaction_id)
            )
            # Recorded with the read, so a view that cannot be audited returns nothing.
            auditor.record(
                connection,
                principal,
                VIEW_TRANSACTION.action,
                object_type=VIEW_TRANSACTION.object_type,
                object_id=transaction_id,
                outcome="success" if transaction is not None else "failed",
                detail=None if transaction is not None else {"reason": "no such Transaction"},
            )
        if transaction is None:
            raise HTTPException(404, "no such Transaction")
        return TransactionView(
            transaction=TransactionOut.of(transaction),
            assessment=None if assessment is None else AssessmentOut.of(assessment),
        )

    @v1.get(
        "/alerts",
        response_model=AlertPage,
        tags=["alerts"],
        responses=_DENIED,
    )
    def get_alerts(
        principal: Annotated[Principal, Depends(require(READ_ALERTS, LIST_ALERTS))],
        auditor: AuditorDep,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        before: Annotated[int | None, Query(description="next_before of the previous page")] = None,
        subject_wallet_id: str | None = None,
    ) -> AlertPage:
        """Alerts, newest first. For Analysts and Supervisors, or the `alerts:read` scope.
        Audited."""
        with engine.begin() as connection:
            found = records.list_alerts(
                connection, limit=limit, before_id=before, subject_wallet_id=subject_wallet_id
            )
            auditor.record(
                connection,
                principal,
                LIST_ALERTS.action,
                object_type=LIST_ALERTS.object_type,
                detail={
                    "limit": limit,
                    "before": before,
                    "subject_wallet_id": subject_wallet_id,
                    "returned": len(found),
                },
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
        `models:read` scope. While the Champion model is down this is `rules-only`, with the
        rules bands."""
        bundle = scoring.active_bundle()
        if bundle is None:
            return ActiveModel(
                model_version=RULES_ONLY,
                thresholds={"medium": rules.bands.medium, "high": rules.bands.high},
                fitted_on=None,
            )
        return ActiveModel(
            model_version=bundle.version,
            thresholds={"medium": bundle.thresholds.medium, "high": bundle.thresholds.high},
            fitted_on=bundle.fitted_on,
        )

    _add_api_key_routes(v1, engine, settings, wall_clock)
    add_case_routes(v1, engine)

    @v1.get("/audit", response_model=AuditPage, tags=["audit"], responses=_DENIED)
    def get_audit(
        principal: Annotated[Principal, Depends(require(READ_AUDIT, READ_AUDIT_LOG))],
        auditor: AuditorDep,
        actor: Annotated[str | None, Query(description="actor_id: a subject ID or key ID")] = None,
        actor_type: Literal["person", "service", "operator"] | None = None,
        action: Annotated[str | None, Query(description="e.g. transaction.view")] = None,
        object_type: str | None = None,
        object_id: str | None = None,
        outcome: Literal["success", "denied", "failed"] | None = None,
        since: Annotated[AwareDatetime | None, Query(description="inclusive")] = None,
        until: Annotated[AwareDatetime | None, Query(description="exclusive")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        before: Annotated[int | None, Query(description="next_before of the previous page")] = None,
    ) -> AuditPage:
        """The audit log, newest first, matching every filter given. Admins only. Reading it is
        itself audited, with the filters used."""
        filters = {
            "actor": actor,
            "actor_type": actor_type,
            "action": action,
            "object_type": object_type,
            "object_id": object_id,
            "outcome": outcome,
            "since": None if since is None else since.isoformat(),
            "until": None if until is None else until.isoformat(),
            "before": before,
            "limit": limit,
        }
        with engine.begin() as connection:
            found = audit.query(
                connection,
                actor_id=actor,
                actor_type=actor_type,
                action=action,
                object_type=object_type,
                object_id=object_id,
                outcome=outcome,
                since=since,
                until=until,
                before_id=before,
                limit=limit,
            )
            auditor.record(
                connection,
                principal,
                READ_AUDIT_LOG.action,
                object_type=READ_AUDIT_LOG.object_type,
                detail={"filters": {k: v for k, v in filters.items() if v is not None}},
            )
        return AuditPage(
            entries=[AuditEntryOut.of(entry) for entry in found],
            next_before=found[-1].audit_id if len(found) == limit else None,
        )

    @v1.get("/health", response_model=Status, tags=["operations"])
    def health() -> Status:
        """Liveness: the process is up. It does not touch the database. Open to all."""
        return Status(status="ok")

    @v1.get(
        "/ready",
        response_model=Readiness,
        tags=["operations"],
        responses={503: {"model": Status, "description": "Not ready to score"}},
    )
    def ready() -> Readiness | JSONResponse:
        """Readiness: the database answers and every migration has been applied. Open to all.

        Degraded mode does not make the service unready, as it can still score: the answer is
        200 with `degraded: true` and the `reason`, so a load balancer keeps sending traffic
        while monitoring sees the Champion model is down."""
        try:
            with engine.connect() as connection:
                pending = pending_migrations(connection)
                waiting = 0 if pending else records.count_awaiting_rescore(connection)
        except sa.exc.SQLAlchemyError:
            return _unavailable("database unreachable")
        if pending:
            return _unavailable(f"migrations not applied: {', '.join(pending)}")
        health = scoring.model_health()
        return Readiness(
            status="ok", degraded=health.degraded, reason=health.reason, awaiting_rescore=waiting
        )

    app.include_router(v1)
    return app


def _add_api_key_routes(
    v1: APIRouter, engine: sa.Engine, settings: ApiSettings, wall_clock: Callable[[], datetime]
) -> None:
    """Issuing, listing, rotating and revoking API keys: for Admins signed in, never for a key.

    Each is audited with the key it concerns, never the key itself. An Admin can issue themselves
    a key that reads Transactions: the audit entry for the issue (who, which key, its scopes) and
    the entries the key then writes under its own key ID are how that is traced.
    """

    def admin(route: AuditedRoute) -> Any:
        return Depends(require(MANAGE_API_KEYS, route))

    unknown: dict[int | str, dict[str, Any]] = {404: {"description": "No such API key"}}

    @v1.post(
        "/api-keys",
        response_model=IssuedApiKeyOut,
        status_code=201,
        tags=["api keys"],
        responses=_DENIED,
    )
    def issue_api_key(
        wanted: ApiKeyIn,
        principal: Annotated[Principal, admin(ISSUE_API_KEY)],
        auditor: AuditorDep,
    ) -> IssuedApiKeyOut:
        """Issue a key for a service. The key is in this response only: just its hash is
        stored. Audited."""
        now = wall_clock()
        limit = wanted.rate_limit_per_minute or settings.api_key_rate_limit
        with engine.begin() as connection:
            issued = api_keys.issue(
                connection,
                name=wanted.name,
                scopes=wanted.scopes,
                rate_limit_per_minute=limit,
                created_by=principal.name,
                now=now,
                expires_at=wanted.expires_at,
            )
            auditor.record(
                connection,
                principal,
                ISSUE_API_KEY.action,
                object_type=ISSUE_API_KEY.object_type,
                object_id=issued.record.key_id,
                detail=_key_detail(issued.record),
            )
        return IssuedApiKeyOut(key=issued.key, api_key=ApiKeyOut.of(issued.record, now))

    @v1.get("/api-keys", response_model=list[ApiKeyOut], tags=["api keys"], responses=_DENIED)
    def list_api_keys(
        principal: Annotated[Principal, admin(LIST_API_KEYS)], auditor: AuditorDep
    ) -> list[ApiKeyOut]:
        """Every key ever issued, revoked and expired ones included, oldest first. Never the
        keys themselves. Audited."""
        now = wall_clock()
        with engine.begin() as connection:
            found = api_keys.list_keys(connection)
            auditor.record(
                connection,
                principal,
                LIST_API_KEYS.action,
                object_type=LIST_API_KEYS.object_type,
                detail={"returned": len(found)},
            )
        return [ApiKeyOut.of(record, now) for record in found]

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
        key_id: str,
        principal: Annotated[Principal, admin(ROTATE_API_KEY)],
        auditor: AuditorDep,
        rotation: Annotated[RotationIn | None, Body()] = None,
    ) -> IssuedApiKeyOut:
        """Issue a replacement with the same name, scopes, rate limit and expiry. The old key
        keeps working for the overlap (a day by default), so the service switches without
        downtime. A key is rotated once; rotate its replacement next time. Audited."""
        minutes = None if rotation is None else rotation.overlap_minutes
        if minutes is None:
            minutes = settings.api_key_rotation_overlap_minutes
        now = wall_clock()

        def failed(reason: str) -> None:
            auditor.record_alone(
                principal,
                ROTATE_API_KEY.action,
                object_type=ROTATE_API_KEY.object_type,
                object_id=key_id,
                outcome="failed",
                detail={"reason": reason},
            )

        try:
            with engine.begin() as connection:
                issued = api_keys.rotate(
                    connection,
                    key_id,
                    overlap=timedelta(minutes=minutes),
                    created_by=principal.name,
                    now=now,
                )
                auditor.record(
                    connection,
                    principal,
                    ROTATE_API_KEY.action,
                    object_type=ROTATE_API_KEY.object_type,
                    object_id=key_id,
                    detail={"overlap_minutes": minutes, **_key_detail(issued.record)},
                )
        except api_keys.UnknownApiKey as e:
            failed("no such API key")
            raise HTTPException(404, "no such API key") from e
        except api_keys.InactiveApiKey as e:
            failed("the API key is revoked or expired")
            raise HTTPException(409, "the API key is revoked or expired; issue a new one") from e
        except api_keys.AlreadyRotated as e:
            failed("the API key has already been rotated")
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
    def revoke_api_key(
        key_id: str, principal: Annotated[Principal, admin(REVOKE_API_KEY)], auditor: AuditorDep
    ) -> ApiKeyOut:
        """Revoke a key at once, as after a leak. Audited."""
        now = wall_clock()
        try:
            with engine.begin() as connection:
                record = api_keys.revoke(connection, key_id, now=now)
                auditor.record(
                    connection,
                    principal,
                    REVOKE_API_KEY.action,
                    object_type=REVOKE_API_KEY.object_type,
                    object_id=key_id,
                    detail={"name": record.name},
                )
        except api_keys.UnknownApiKey as e:
            auditor.record_alone(
                principal,
                REVOKE_API_KEY.action,
                object_type=REVOKE_API_KEY.object_type,
                object_id=key_id,
                outcome="failed",
                detail={"reason": "no such API key"},
            )
            raise HTTPException(404, "no such API key") from e
        return ApiKeyOut.of(record, now)


def _key_detail(record: api_keys.ApiKeyRecord) -> dict[str, Any]:
    """What an audit entry says about a key: who holds it and what it may do, never the key."""
    return {
        "name": record.name,
        "scopes": sorted(record.scopes),
        "rate_limit_per_minute": record.rate_limit_per_minute,
        "expires_at": None if record.expires_at is None else record.expires_at.isoformat(),
        "replaces": record.replaces,
    }


def _unavailable(detail: str) -> JSONResponse:
    return JSONResponse(Status(status="unavailable", detail=detail).model_dump(), status_code=503)


def app_from_env() -> FastAPI:
    """The API configured from `TXRISK_*` environment variables, for `uvicorn --factory`."""
    return create_app(ApiSettings.from_env())
