"""The HTTP API, under `/v1`, with its OpenAPI spec at `/v1/openapi.json`.

The API never changes the schema: `txrisk migrate` does that as its own job, and readiness
reports unready until it has.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from txrisk.api.schemas import (
    ActiveModel,
    AlertOut,
    AlertPage,
    AssessmentOut,
    Status,
    TransactionIn,
    TransactionOut,
    TransactionView,
)
from txrisk.api.settings import ApiSettings
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import RULES_ONLY
from txrisk.store import (
    SimulationClock,
    StoredScoring,
    TransactionConflict,
    UnknownParty,
    create_engine,
    pending_migrations,
    records,
)

# Set on a scoring response that returns the assessment an earlier call stored.
REPLAYED_HEADER = "Idempotent-Replayed"


def create_app(settings: ApiSettings) -> FastAPI:
    """The API over the database and model bundle in `settings`.

    The bundle is loaded once, here; a configured bundle that cannot be loaded stops the API from
    starting rather than letting it score with something other than what was configured.
    """
    engine = create_engine(settings.database_url)
    bundle = None if settings.model_bundle is None else ModelBundle.load(settings.model_bundle)
    rules = settings.rules()
    scoring = StoredScoring(engine, features=settings.features(), rules=rules, bundle=bundle)
    clock = SimulationClock(engine)
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

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Transaction risk scoring",
        version="1",
        description=(
            "Scores mobile money Transactions for risk and explains why. Advisory only: it "
            "raises Alerts for people to review and never blocks a Transaction."
        ),
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    v1 = APIRouter(prefix="/v1")

    @v1.post("/score", response_model=AssessmentOut, tags=["scoring"])
    def score(transaction: TransactionIn, response: Response) -> AssessmentOut:
        """Score a Transaction and store it with its Risk assessment; a High Risk band raises
        an Alert. Idempotent on `transaction_id`: a repeat returns the stored assessment, marked
        with the `Idempotent-Replayed: true` header, and raises no second Alert."""
        try:
            scored = scoring.assess(transaction.to_transaction())
        except UnknownParty as e:
            raise HTTPException(422, f"not registered: {e}") from e
        except TransactionConflict as e:
            raise HTTPException(409, f"a different Transaction is already stored as {e}") from e
        # The simulation clock follows the latest event time seen (ADR 0005).
        clock.advance(transaction.occurred_at)
        if scored.replayed:
            response.headers[REPLAYED_HEADER] = "true"
        return AssessmentOut.of(scored.assessment)

    @v1.get(
        "/transactions/{transaction_id}",
        response_model=TransactionView,
        tags=["transactions"],
        responses={404: {"description": "No such Transaction"}},
    )
    def get_transaction(transaction_id: str) -> TransactionView:
        """A stored Transaction and its Risk assessment, if it was scored."""
        with engine.connect() as connection:
            transaction = records.stored_transaction(connection, transaction_id)
            if transaction is None:
                raise HTTPException(404, "no such Transaction")
            assessment = records.stored_assessment(connection, transaction_id)
        return TransactionView(
            transaction=TransactionOut.of(transaction),
            assessment=None if assessment is None else AssessmentOut.of(assessment),
        )

    @v1.get("/alerts", response_model=AlertPage, tags=["alerts"])
    def get_alerts(
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        before: Annotated[int | None, Query(description="next_before of the previous page")] = None,
        subject_wallet_id: str | None = None,
    ) -> AlertPage:
        """Alerts, newest first."""
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

    @v1.get("/models/active", response_model=ActiveModel, tags=["models"])
    def get_active_model() -> ActiveModel:
        """The model version that scores, and its Risk band thresholds."""
        return active

    @v1.get("/health", response_model=Status, tags=["operations"])
    def health() -> Status:
        """Liveness: the process is up. It does not touch the database."""
        return Status(status="ok")

    @v1.get(
        "/ready",
        response_model=Status,
        tags=["operations"],
        responses={503: {"model": Status, "description": "Not ready to score"}},
    )
    def ready() -> Status | JSONResponse:
        """Readiness: the database answers and every migration has been applied."""
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


def _unavailable(detail: str) -> JSONResponse:
    return JSONResponse(Status(status="unavailable", detail=detail).model_dump(), status_code=503)


def app_from_env() -> FastAPI:
    """The API configured from `TXRISK_*` environment variables, for `uvicorn --factory`."""
    return create_app(ApiSettings.from_env())
