"""Fraud labels: what the system knows about fraud, and only from the moment it came to know it.

A label is stored from a `fraud.discovered` event with that event's own time. Ground truth never
comes here (ADR 0007). Anything that reads labels asks for those `known_at` a moment, so a
discovery is invisible before its event time however early it was stored.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.store.tables import fraud_labels


@dataclass(frozen=True)
class FraudLabel:
    transaction_id: str
    discovered_at: datetime
    source: str


class FraudLabels:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def record(self, transaction_id: str, discovered_at: datetime, source: str) -> bool:
        """Store the label; False if the Transaction already has one. The first discovery stands:
        a repeated event, or a later discovery of the same Transaction, changes nothing."""
        with self._engine.begin() as connection:
            inserted = connection.execute(
                insert(fraud_labels)
                .values(transaction_id=transaction_id, discovered_at=discovered_at, source=source)
                .on_conflict_do_nothing()
                .returning(fraud_labels.c.transaction_id)
            ).first()
        return inserted is not None

    def known_at(self, moment: datetime) -> list[FraudLabel]:
        """The labels discovered at or before `moment`, oldest discovery first."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(fraud_labels)
                .where(fraud_labels.c.discovered_at <= moment)
                .order_by(fraud_labels.c.discovered_at, fraud_labels.c.transaction_id)
            )
            return [FraudLabel(r.transaction_id, r.discovered_at, r.source) for r in rows]
