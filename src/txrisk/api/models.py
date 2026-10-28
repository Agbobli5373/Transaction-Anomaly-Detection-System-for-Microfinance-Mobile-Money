"""Model versions, their cards, and Promotion and rollback of the Champion model (`/v1/models`).

Anyone who may read models (every role, or the `models:read` scope) lists them and reads a
version's model card. Only an Admin signed in checks the gates, promotes or rolls back, and the
API does not take the caller's word that the gates pass: it runs both of them itself, at the
simulated time, every time Promotion is asked for, and refuses with 409 and the report if either
fails. Promotion and rollback are audited, the refused ones too, with the gate results.

A Promotion moves the `champion_model` pointer in Postgres; the workers and the API follow it
within `challenger_refresh_seconds`, with no restart (see `txrisk.store.champion`).
"""

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as PathParam

from txrisk.api.audit import AuditedRoute, AuditorDep
from txrisk.api.auth import PROMOTE_MODELS, READ_MODELS, Principal, require
from txrisk.api.schemas import (
    GatesOut,
    GatesRefused,
    ModelCardOut,
    ModelList,
    ModelVersionOut,
    PromotionOut,
)
from txrisk.api.settings import ApiSettings
from txrisk.models.bundle import ModelBundle, bundle_version
from txrisk.models.card import load_card, render_markdown
from txrisk.promotion.gates import GateReport, check_gates
from txrisk.scoring import RulesConfig
from txrisk.store import SimulationClock, challenger_version
from txrisk.store import promotions as store
from txrisk.store.champion import champion_pointer

log = logging.getLogger(__name__)

_DENIED: dict[int | str, dict[str, Any]] = {
    401: {"description": "No credential, or an invalid, expired or revoked one"},
    403: {"description": "The caller's roles or scopes do not permit this"},
}

CHECK_GATES = AuditedRoute("model.gates", "model", "version")
PROMOTE_MODEL = AuditedRoute("model.promote", "model", "version")
ROLL_BACK_MODEL = AuditedRoute("model.rollback", "model")

# A bundle version as a path segment: never a path.
Version = Annotated[str, PathParam(pattern=r"^\w[\w.-]{0,127}$")]


class _Refused(Exception):
    """A Promotion cannot be considered, or does not pass: why, and the gates if they were run."""

    def __init__(self, message: str, gates: GateReport | None = None) -> None:
        super().__init__(message)
        self.gates = gates


def add_model_routes(
    v1: APIRouter,
    engine: sa.Engine,
    settings: ApiSettings,
    rules: RulesConfig,
    wall_clock: Callable[[], datetime],
) -> None:
    models = settings.models_folder()
    configured = settings.model_bundle
    configured_version: list[str] = []  # read once, when first needed

    def first_champion() -> str | None:
        """The version of the bundle the processes are configured with, the first Champion."""
        if configured is None:
            return None
        if not configured_version:
            try:
                configured_version.append(bundle_version(configured))
            except Exception:
                log.warning("could not read the configured model bundle", exc_info=True)
                return None
        return configured_version[0]

    def champion_version(pointer: str | None) -> str:
        version = pointer or first_champion()
        if version is None:
            raise _Refused("no Champion model is configured, so there is nothing to compare with")
        return version

    def folder_of(version: str) -> Path | None:
        if version == first_champion():
            return configured
        if models is not None and (models / version).is_dir():
            return models / version
        return None

    def load(version: str) -> ModelBundle:
        folder = folder_of(version)
        if folder is None:
            raise _Refused(f"no model bundle {version} is saved")
        try:
            return ModelBundle.load(folder)
        except Exception as e:
            raise _Refused(f"model bundle {version} could not be loaded: {e}") from e

    def check(version: str) -> tuple[str | None, GateReport]:
        """The Champion model pointer and both gates for `version`, or why it cannot be judged."""
        if challenger_version(engine) != version:
            raise _Refused(
                f"{version} is not the Challenger model: only the Challenger scores in shadow, "
                "so only it can pass the shadow gate"
            )
        pointer = champion_pointer(engine)
        current = champion_version(pointer)
        if current == version:
            raise _Refused(f"{version} is already the Champion model")
        as_of = SimulationClock(engine).peek()
        if as_of is None:
            raise _Refused("the simulation clock has not started, so no window has matured")
        report = check_gates(
            engine,
            champion=load(current),
            challenger=load(version),
            rules=rules,
            config=settings.promotion(),
            evaluation=settings.evaluation(),
            as_of=as_of,
        )
        return pointer, report

    @v1.get(
        "/models",
        response_model=ModelList,
        tags=["models"],
        dependencies=[Depends(require(READ_MODELS))],
        responses=_DENIED,
    )
    def list_models() -> ModelList:
        """The Champion and Challenger models, every version retraining has made, and the latest
        Promotions and rollbacks. For every role, or the `models:read` scope."""
        with engine.connect() as connection:
            pointer = champion_pointer(engine)
            versions = store.known_versions(connection)
            history = store.history(connection)
        champion = pointer or first_champion()
        challenger = challenger_version(engine)
        by_version = {v.version: v for v in versions}
        order = [*dict.fromkeys(v for v in (champion, challenger, *by_version) if v is not None)]
        listed = []
        for version in order:
            roles: list[Literal["champion", "challenger"]] = []
            if version == champion:
                roles.append("champion")
            if version == challenger:
                roles.append("challenger")
            run = by_version.get(version)
            listed.append(
                ModelVersionOut(
                    version=version,
                    roles=roles,
                    trained_cutoff=None if run is None else run.trained_cutoff,
                    trigger=None if run is None else run.trigger,
                )
            )
        return ModelList(
            champion=champion,
            challenger=challenger,
            models=listed,
            history=[PromotionOut.model_validate(h, from_attributes=True) for h in history],
        )

    @v1.get(
        "/models/{version}/card",
        response_model=ModelCardOut,
        tags=["models"],
        dependencies=[Depends(require(READ_MODELS))],
        responses={**_DENIED, 404: {"description": "No such model version"}},
    )
    def get_model_card(version: Version) -> ModelCardOut:
        """The model card of a version: intended use, the data it learned from, its metrics by
        segment and its limitations, as data and as a Markdown page. For every role, or the
        `models:read` scope."""
        folder = folder_of(version)
        if folder is None:
            raise HTTPException(404, "no such model version")
        try:
            card = load_card(folder)
        except FileNotFoundError as e:
            raise HTTPException(404, "no such model version") from e
        return ModelCardOut(version=version, card=card, markdown=render_markdown(card))

    @v1.get(
        "/models/{version}/gates",
        response_model=GatesOut,
        tags=["models"],
        responses={**_DENIED, 409: {"description": "Cannot be judged: `detail` says why"}},
    )
    def get_gates(
        version: Version,
        principal: Annotated[Principal, Depends(require(PROMOTE_MODELS, CHECK_GATES))],
        auditor: AuditorDep,
    ) -> GatesOut:
        """Run the backtest and shadow gates for the Challenger model now, without promoting it.
        Admins only. Audited."""
        try:
            _, report = check(version)
        except _Refused as e:
            auditor.record_alone(
                principal,
                CHECK_GATES.action,
                object_type=CHECK_GATES.object_type,
                object_id=version,
                outcome="failed",
                detail={"reason": str(e)},
            )
            raise HTTPException(409, GatesRefused(message=str(e)).model_dump(mode="json")) from e
        auditor.record_alone(
            principal,
            CHECK_GATES.action,
            object_type=CHECK_GATES.object_type,
            object_id=version,
            detail={"passed": report.passed},
        )
        return GatesOut.model_validate(report.to_json())

    @v1.post(
        "/models/{version}/promote",
        response_model=PromotionOut,
        tags=["models"],
        responses={
            **_DENIED,
            409: {
                "description": "Not the Challenger model, or a gate did not pass, or the "
                "Champion model changed meanwhile: `detail` has the `message` and the `gates`"
            },
        },
    )
    def promote(
        version: Version,
        principal: Annotated[Principal, Depends(require(PROMOTE_MODELS, PROMOTE_MODEL))],
        auditor: AuditorDep,
    ) -> PromotionOut:
        """Make the Challenger model the Champion model, if it passes both gates, which are
        checked here, now, whatever anyone has seen before. Admins only; manual, never
        automatic. The workers and the API switch within a few seconds. Audited with the gate
        results, whether or not it is allowed."""

        def refuse(message: str, gates: GateReport | None) -> HTTPException:
            detail: dict[str, Any] = {"reason": message}
            if gates is not None:
                detail["gates"] = gates.to_json()
            auditor.record_alone(
                principal,
                PROMOTE_MODEL.action,
                object_type=PROMOTE_MODEL.object_type,
                object_id=version,
                outcome="failed",
                detail=detail,
            )
            refused = GatesRefused(
                message=message,
                gates=None if gates is None else GatesOut.model_validate(gates.to_json()),
            )
            return HTTPException(409, refused.model_dump(mode="json"))

        try:
            pointer, report = check(version)
        except _Refused as e:
            raise refuse(str(e), e.gates) from e
        if not report.passed:
            failures = [*report.backtest.failures, *report.shadow.failures]
            raise refuse("the gates did not pass: " + "; ".join(failures), report)
        try:
            with engine.begin() as connection:
                record = store.promote(
                    connection,
                    version=version,
                    champion_version=report.champion_version,
                    expected_pointer=pointer,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                    occurred_at=wall_clock(),
                    gates=report.to_json(),
                )
                auditor.record(
                    connection,
                    principal,
                    PROMOTE_MODEL.action,
                    object_type=PROMOTE_MODEL.object_type,
                    object_id=version,
                    detail={"from_version": report.champion_version, "gates": report.to_json()},
                )
        except store.ChampionChanged as e:
            raise refuse(str(e), report) from e
        return PromotionOut.model_validate(record, from_attributes=True)

    @v1.post(
        "/models/rollback",
        response_model=PromotionOut,
        tags=["models"],
        responses={
            **_DENIED,
            409: {"description": "There is no Promotion to roll back: `detail` says so"},
        },
    )
    def roll_back(
        principal: Annotated[Principal, Depends(require(PROMOTE_MODELS, ROLL_BACK_MODEL))],
        auditor: AuditorDep,
    ) -> PromotionOut:
        """Put back the Champion model that the latest Promotion replaced, in one call and with
        no gates (a bad Promotion must be quick to undo). One step: it cannot be repeated until
        there is another Promotion. Admins only. Audited."""
        try:
            with engine.begin() as connection:
                record = store.roll_back(
                    connection,
                    actor_id=principal.subject,
                    actor_name=principal.name,
                    occurred_at=wall_clock(),
                )
                auditor.record(
                    connection,
                    principal,
                    ROLL_BACK_MODEL.action,
                    object_type=ROLL_BACK_MODEL.object_type,
                    object_id=record.to_version,
                    detail={"from_version": record.from_version, "to_version": record.to_version},
                )
        except store.NothingToRollBack as e:
            auditor.record_alone(
                principal,
                ROLL_BACK_MODEL.action,
                object_type=ROLL_BACK_MODEL.object_type,
                outcome="failed",
                detail={"reason": str(e)},
            )
            raise HTTPException(409, GatesRefused(message=str(e)).model_dump(mode="json")) from e
        return PromotionOut.model_validate(record, from_attributes=True)
