"""The Medium watch list: Transactions whose Risk band is Medium. They raise no Alert, so no Case;
people scan them for patterns that have not yet reached High."""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa

from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store.history import select_transactions, transaction_from_row
from txrisk.store.records import assessment_from_row
from txrisk.store.tables import risk_assessments, transactions


@dataclass(frozen=True)
class WatchedTransaction:
    transaction: Transaction
    assessment: RiskAssessment


def list_medium(
    connection: sa.Connection,
    *,
    limit: int,
    before: tuple[datetime, str] | None = None,
    subject_wallet_id: str | None = None,
    region: str | None = None,
    transaction_type: str | None = None,
) -> list[WatchedTransaction]:
    """Medium-band Transactions, newest first by event time (the Transaction ID breaks ties);
    `before` is the (event time, ID) of the last one of the previous page."""
    query = (
        select_transactions()
        .add_columns(*risk_assessments.c)
        .join(risk_assessments, risk_assessments.c.transaction_id == transactions.c.transaction_id)
        .where(risk_assessments.c.risk_band == "Medium")
        .order_by(transactions.c.occurred_at.desc(), transactions.c.transaction_id.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(
            sa.tuple_(transactions.c.occurred_at, transactions.c.transaction_id)
            < sa.tuple_(*before)
        )
    if subject_wallet_id is not None:
        query = query.where(transactions.c.subject_wallet_id == subject_wallet_id)
    if region is not None:
        query = query.where(transactions.c.region == region)
    if transaction_type is not None:
        query = query.where(transactions.c.transaction_type == transaction_type)
    return [
        WatchedTransaction(transaction_from_row(row[:11]), assessment_from_row(row))
        for row in connection.execute(query)
    ]
