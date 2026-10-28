"""The retention purge: delete what is older than its configured retention period.

Ages are measured in event time, from the simulation clock (ADR 0005), never the wall clock, so a
replayed month ages data as a month. The audit log is kept longest and is the exception to both
rules: its entries are stamped with the wall clock, so their age is measured on the wall clock
too, by the database (`audit_log_purge`, migration 0004), and no configuration can keep them less
than a year. The purge must run as the owner role: the application role has no DELETE anywhere.
"""

import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Self

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator

from txrisk.store.clock import SimulationClock
from txrisk.store.tables import (
    alerts,
    fraud_labels,
    risk_assessments,
    sim_swaps,
    transactions,
)


class RetentionDays(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transactions: int = Field(ge=1)
    risk_assessments: int = Field(ge=1)
    alerts: int = Field(ge=1)
    sim_swaps: int = Field(ge=1)
    fraud_labels: int = Field(ge=1)
    # Days of real time, and at least a year: the database refuses less (see migration 0004).
    audit_log: int = Field(ge=365)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        # Foreign keys: an assessment belongs to a Transaction, an Alert to an assessment.
        if not self.alerts <= self.risk_assessments <= self.transactions:
            raise ValueError(
                "alerts must be kept no longer than risk_assessments, "
                "and risk_assessments no longer than transactions"
            )
        others = max(
            self.transactions,
            self.risk_assessments,
            self.alerts,
            self.sim_swaps,
            self.fraud_labels,
        )
        if self.audit_log <= others:
            raise ValueError(f"audit_log must be kept longer than every other table ({others})")
        return self


class RetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    days: RetentionDays


def load_retention_config(path: Path) -> RetentionConfig:
    with path.open("rb") as f:
        return RetentionConfig.model_validate(tomllib.load(f))


# What is purged, dependants first so no foreign key is ever violated: the table, its event-time
# column and its field in `RetentionDays`. Wallets, Agents, Merchants and Billers are not purged.
_PURGED = (
    (alerts, alerts.c.raised_at, "alerts"),
    (risk_assessments, risk_assessments.c.assessed_at, "risk_assessments"),
    (transactions, transactions.c.occurred_at, "transactions"),
    (sim_swaps, sim_swaps.c.occurred_at, "sim_swaps"),
    (fraud_labels, fraud_labels.c.discovered_at, "fraud_labels"),
)


@dataclass(frozen=True)
class Purged:
    """The clock reading the purge ran at, and the rows deleted per table."""

    at: datetime
    deleted: dict[str, int]


def purge(engine: sa.Engine, clock: SimulationClock, config: RetentionConfig) -> Purged | None:
    """Delete every row older than its table's retention period before the clock's reading.

    A row exactly one period old is kept. Returns None, deleting nothing, before the clock has a
    reading. Everything is deleted in one transaction, so a failure purges nothing. Audit entries
    older than their period go too, by wall-clock age, through `audit_log_purge`.
    """
    at = clock.peek()
    if at is None:
        return None
    deleted: dict[str, int] = {}
    with engine.begin() as connection:
        for table, column, name in _PURGED:
            cutoff = at - timedelta(days=getattr(config.days, name))
            result = connection.execute(sa.delete(table).where(column < cutoff))
            deleted[name] = result.rowcount
        # The audit log's only deletion path: a function the owner runs, which keeps what is
        # younger than the period and refuses a period under a year.
        deleted["audit_log"] = connection.execute(
            sa.text("SELECT audit_log_purge(make_interval(days => :days))"),
            {"days": config.days.audit_log},
        ).scalar_one()
    return Purged(at=at, deleted=deleted)
