"""Promotion and rollback in Postgres: the Champion model pointer and the history of every change.

A Promotion makes a Challenger model the Champion model; a rollback puts back the Champion model
before it, in one step. Both move the `champion_model` pointer and append a row to
`model_promotions` in the same transaction, so the pointer never moves without a record of who
moved it. Whether the gates allow a Promotion is decided by the caller (`txrisk.promotion`): this
module only keeps state, and enforces that a rollback is one step.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from txrisk.store.tables import champion_model, model_promotions, retraining_runs


class NothingToRollBack(Exception):
    """The latest change was not a Promotion, or there has been none."""


class ChampionChanged(Exception):
    """The Champion model is no longer the one the caller checked the gates against."""


@dataclass(frozen=True)
class PromotionRecord:
    promotion_id: int
    action: str
    from_version: str
    to_version: str
    actor_name: str
    occurred_at: datetime
    gates: dict[str, Any]


@dataclass(frozen=True)
class ModelVersion:
    version: str
    # When the training set was cut, and what started the run; None for a version retraining did
    # not make (the first Champion).
    trained_cutoff: datetime | None
    trigger: str | None


def lock_pointer(connection: sa.Connection) -> str | None:
    """The Champion model pointer, locking its row until the transaction ends, so two Promotions
    cannot interleave."""
    pointer: str | None = connection.execute(
        sa.select(champion_model.c.bundle_version).with_for_update()
    ).scalar_one()
    return pointer


def _record(row: sa.Row[Any]) -> PromotionRecord:
    return PromotionRecord(
        promotion_id=row.promotion_id,
        action=row.action,
        from_version=row.from_version,
        to_version=row.to_version,
        actor_name=row.actor_name,
        occurred_at=row.occurred_at,
        gates=row.gates,
    )


def latest(connection: sa.Connection) -> PromotionRecord | None:
    """The most recent Promotion or rollback."""
    row = connection.execute(
        sa.select(model_promotions).order_by(model_promotions.c.promotion_id.desc()).limit(1)
    ).first()
    return None if row is None else _record(row)


def history(connection: sa.Connection, *, limit: int = 50) -> list[PromotionRecord]:
    """The latest Promotions and rollbacks, newest first."""
    rows = connection.execute(
        sa.select(model_promotions).order_by(model_promotions.c.promotion_id.desc()).limit(limit)
    )
    return [_record(r) for r in rows]


def _move(
    connection: sa.Connection,
    *,
    action: str,
    from_version: str,
    to_version: str,
    from_pointer: str | None,
    to_pointer: str | None,
    actor_id: str,
    actor_name: str,
    occurred_at: datetime,
    gates: dict[str, Any],
) -> PromotionRecord:
    row = connection.execute(
        sa.insert(model_promotions)
        .values(
            action=action,
            from_version=from_version,
            to_version=to_version,
            from_pointer=from_pointer,
            actor_id=actor_id,
            actor_name=actor_name,
            occurred_at=occurred_at,
            gates=gates,
        )
        .returning(*model_promotions.c)
    ).one()
    connection.execute(
        sa.update(champion_model)
        .where(champion_model.c.singleton)
        .values(bundle_version=to_pointer, promotion_id=row.promotion_id)
    )
    return _record(row)


def promote(
    connection: sa.Connection,
    *,
    version: str,
    champion_version: str,
    expected_pointer: str | None,
    actor_id: str,
    actor_name: str,
    occurred_at: datetime,
    gates: dict[str, Any],
) -> PromotionRecord:
    """Make `version` the Champion model, in `connection`'s transaction. `expected_pointer` is
    the pointer the gates were checked against: if the Champion model has changed since, nothing
    is promoted (`ChampionChanged`)."""
    pointer = lock_pointer(connection)
    if pointer != expected_pointer:
        raise ChampionChanged(
            "the Champion model changed while the gates were being checked; check them again"
        )
    return _move(
        connection,
        action="promote",
        from_version=champion_version,
        to_version=version,
        from_pointer=pointer,
        to_pointer=version,
        actor_id=actor_id,
        actor_name=actor_name,
        occurred_at=occurred_at,
        gates=gates,
    )


def roll_back(
    connection: sa.Connection, *, actor_id: str, actor_name: str, occurred_at: datetime
) -> PromotionRecord:
    """Put back the Champion model before the latest Promotion, in `connection`'s transaction.
    One step: the rollback is itself the latest change, so a second call raises
    `NothingToRollBack` until there is a new Promotion."""
    pointer = lock_pointer(connection)
    last = connection.execute(
        sa.select(model_promotions).order_by(model_promotions.c.promotion_id.desc()).limit(1)
    ).first()
    if last is None or last.action != "promote":
        raise NothingToRollBack(
            "there is no Promotion to roll back"
            if last is None
            else "the latest change was already a rollback"
        )
    return _move(
        connection,
        action="rollback",
        from_version=last.to_version,
        to_version=last.from_version,
        from_pointer=pointer,
        to_pointer=last.from_pointer,
        actor_id=actor_id,
        actor_name=actor_name,
        occurred_at=occurred_at,
        gates={},
    )


def known_versions(connection: sa.Connection) -> list[ModelVersion]:
    """Every version retraining has made, newest first."""
    rows = connection.execute(
        sa.select(
            retraining_runs.c.bundle_version, retraining_runs.c.cutoff, retraining_runs.c.trigger
        ).order_by(retraining_runs.c.run_id.desc())
    )
    seen: dict[str, ModelVersion] = {}
    for r in rows:
        seen.setdefault(r.bundle_version, ModelVersion(r.bundle_version, r.cutoff, r.trigger))
    return list(seen.values())
