"""Reading and writing Transactions, parties, Risk assessments and Alerts."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.features import Transaction
from txrisk.scoring import Reason, RiskAssessment, RiskBand
from txrisk.store.history import select_transactions, transaction_from_row
from txrisk.store.tables import (
    COUNTERPARTY_COLUMNS,
    agents,
    alerts,
    billers,
    merchants,
    risk_assessment_history,
    risk_assessments,
    sim_swaps,
    transactions,
    wallets,
)


class TransactionConflict(Exception):
    """A different Transaction is already stored under this Transaction ID."""


class UnknownParty(Exception):
    """The Transaction names a Wallet, Agent, Merchant or Biller that is not registered."""


@dataclass(frozen=True)
class AlertRecord:
    alert_id: int
    transaction_id: str
    subject_wallet_id: str
    raised_at: datetime
    assessment: RiskAssessment


def add_wallet(
    connection: sa.Connection,
    wallet_id: str,
    *,
    kyc_tier: str,
    home_region: str,
    registered_at: datetime,
) -> None:
    connection.execute(
        insert(wallets)
        .values(
            wallet_id=wallet_id,
            kyc_tier=kyc_tier,
            home_region=home_region,
            registered_at=registered_at,
        )
        .on_conflict_do_nothing()
    )


def add_agent(connection: sa.Connection, agent_id: str, *, region: str) -> None:
    connection.execute(
        insert(agents).values(agent_id=agent_id, region=region).on_conflict_do_nothing()
    )


def add_sim_swap(connection: sa.Connection, wallet_id: str, occurred_at: datetime) -> bool:
    """Store the SIM swap; False if it was already stored."""
    inserted = connection.execute(
        insert(sim_swaps)
        .values(wallet_id=wallet_id, occurred_at=occurred_at)
        .on_conflict_do_nothing()
        .returning(sim_swaps.c.wallet_id)
    ).first()
    return inserted is not None


def wallet_exists(connection: sa.Connection, wallet_id: str) -> bool:
    found = connection.execute(
        sa.select(sa.literal(1)).where(wallets.c.wallet_id == wallet_id)
    ).first()
    return found is not None


# The key each Counterparty column refers to.
_PARTY_KEYS = {
    "counterparty_wallet_id": wallets.c.wallet_id,
    "agent_id": agents.c.agent_id,
    "merchant_id": merchants.c.merchant_id,
    "biller_id": billers.c.biller_id,
}


def unknown_parties(connection: sa.Connection, transaction: Transaction) -> list[str]:
    """What the Transaction names that is not registered, as `column=value`."""
    column = COUNTERPARTY_COLUMNS[transaction.transaction_type]
    named = [
        ("subject_wallet_id", wallets.c.wallet_id, transaction.subject_wallet_id),
        (column, _PARTY_KEYS[column], transaction.counterparty_id),
    ]
    missing = []
    for name, key, value in named:
        found = connection.execute(sa.select(sa.literal(1)).where(key == value)).first()
        if found is None:
            missing.append(f"{name}={value}")
    return missing


def _transaction_values(transaction: Transaction) -> dict[str, object]:
    return {
        "transaction_id": transaction.transaction_id,
        "occurred_at": transaction.occurred_at,
        "transaction_type": transaction.transaction_type,
        "amount": transaction.amount,
        "subject_wallet_id": transaction.subject_wallet_id,
        "direction": transaction.direction,
        "subject_balance_before": transaction.subject_balance_before,
        "channel": transaction.channel,
        "device_id": transaction.device_id,
        "region": transaction.region,
        COUNTERPARTY_COLUMNS[transaction.transaction_type]: transaction.counterparty_id,
    }


def stored_transaction(connection: sa.Connection, transaction_id: str) -> Transaction | None:
    row = connection.execute(
        select_transactions().where(transactions.c.transaction_id == transaction_id)
    ).one_or_none()
    return None if row is None else transaction_from_row(row)


def record_transaction(connection: sa.Connection, transaction: Transaction) -> bool:
    """Store the Transaction; False if it was already stored. Raises `TransactionConflict` if a
    different Transaction is stored under its ID."""
    inserted = connection.execute(
        insert(transactions)
        .values(**_transaction_values(transaction))
        .on_conflict_do_nothing()
        .returning(transactions.c.transaction_id)
    ).first()
    if inserted is not None:
        return True
    if stored_transaction(connection, transaction.transaction_id) != transaction:
        raise TransactionConflict(transaction.transaction_id)
    return False


def _json_number(value: float | None) -> float | None:
    """JSON has no NaN or infinity; store them as null."""
    return None if value is None or not math.isfinite(value) else value


def save_assessment(
    connection: sa.Connection, assessment: RiskAssessment, *, assessed_at: datetime
) -> bool:
    """Store the assessment; False, storing nothing, if one is already stored for it."""
    inserted = connection.execute(
        insert(risk_assessments)
        .values(
            transaction_id=assessment.transaction_id,
            **_assessment_values(assessment),
            assessed_at=assessed_at,
        )
        .on_conflict_do_nothing()
        .returning(risk_assessments.c.transaction_id)
    ).first()
    return inserted is not None


def _assessment_values(assessment: RiskAssessment) -> dict[str, Any]:
    return {
        "risk_score": assessment.risk_score,
        "risk_band": assessment.risk_band.value,
        "reasons": [
            {
                "code": r.code,
                "message": r.message,
                "value": r.value if isinstance(r.value, str) else _json_number(r.value),
            }
            for r in assessment.reasons
        ],
        "component_scores": {k: _json_number(v) for k, v in assessment.component_scores.items()},
        "model_version": assessment.model_version,
        "degraded": assessment.degraded,
    }


def _assessment(row: sa.Row[Any]) -> RiskAssessment:
    return RiskAssessment(
        transaction_id=row.transaction_id,
        risk_score=row.risk_score,
        risk_band=RiskBand(row.risk_band),
        reasons=tuple(Reason(r["code"], r["message"], r["value"]) for r in row.reasons),
        component_scores=dict(row.component_scores),
        model_version=row.model_version,
        degraded=row.degraded,
    )


def stored_assessment(connection: sa.Connection, transaction_id: str) -> RiskAssessment | None:
    row = connection.execute(
        sa.select(risk_assessments).where(risk_assessments.c.transaction_id == transaction_id)
    ).one_or_none()
    return None if row is None else _assessment(row)


def awaiting_rescore(connection: sa.Connection, *, limit: int | None = None) -> list[str]:
    """Transaction IDs whose stored assessment is degraded, oldest event time first."""
    query = (
        sa.select(risk_assessments.c.transaction_id)
        .where(risk_assessments.c.degraded)
        .order_by(risk_assessments.c.assessed_at, risk_assessments.c.transaction_id)
    )
    if limit is not None:
        query = query.limit(limit)
    return list(connection.execute(query).scalars())


def count_awaiting_rescore(connection: sa.Connection) -> int:
    return int(
        connection.execute(
            sa.select(sa.func.count())
            .select_from(risk_assessments)
            .where(risk_assessments.c.degraded)
        ).scalar_one()
    )


def replace_degraded_assessment(connection: sa.Connection, assessment: RiskAssessment) -> bool:
    """Replace the Transaction's degraded assessment with `assessment`, keeping the old one in
    the history; False, changing nothing, if it is not degraded (another re-score got there
    first, or it was never degraded). The assessment time stays the Transaction's event time."""
    current = connection.execute(
        sa.select(risk_assessments)
        .where(risk_assessments.c.transaction_id == assessment.transaction_id)
        .with_for_update()
    ).one_or_none()
    if current is None or not current.degraded:
        return False
    connection.execute(
        insert(risk_assessment_history).values(
            transaction_id=current.transaction_id,
            risk_score=current.risk_score,
            risk_band=current.risk_band,
            reasons=current.reasons,
            component_scores=current.component_scores,
            model_version=current.model_version,
            degraded=current.degraded,
            assessed_at=current.assessed_at,
            replaced_by=assessment.model_version,
        )
    )
    connection.execute(
        sa.update(risk_assessments)
        .where(risk_assessments.c.transaction_id == assessment.transaction_id)
        .values(**_assessment_values(assessment))
    )
    return True


def assessment_history(connection: sa.Connection, transaction_id: str) -> list[RiskAssessment]:
    """The assessments re-scoring replaced, oldest first."""
    rows = connection.execute(
        sa.select(risk_assessment_history)
        .where(risk_assessment_history.c.transaction_id == transaction_id)
        .order_by(risk_assessment_history.c.history_id)
    )
    return [_assessment(row) for row in rows]


def raise_alert(connection: sa.Connection, transaction: Transaction) -> bool:
    """Queue an Alert for the Transaction, raised at its event time; False if it has one."""
    inserted = connection.execute(
        insert(alerts)
        .values(
            transaction_id=transaction.transaction_id,
            subject_wallet_id=transaction.subject_wallet_id,
            raised_at=transaction.occurred_at,
        )
        .on_conflict_do_nothing(index_elements=[alerts.c.transaction_id])
        .returning(alerts.c.alert_id)
    ).first()
    return inserted is not None


def list_alerts(
    connection: sa.Connection,
    *,
    limit: int,
    before_id: int | None = None,
    subject_wallet_id: str | None = None,
) -> list[AlertRecord]:
    """Alerts newest first, with their assessments; `before_id` continues a previous page."""
    query = (
        sa.select(
            alerts.c.alert_id,
            alerts.c.subject_wallet_id,
            alerts.c.raised_at,
            *risk_assessments.c,
        )
        .join(risk_assessments, risk_assessments.c.transaction_id == alerts.c.transaction_id)
        .order_by(alerts.c.alert_id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.where(alerts.c.alert_id < before_id)
    if subject_wallet_id is not None:
        query = query.where(alerts.c.subject_wallet_id == subject_wallet_id)
    return [
        AlertRecord(
            alert_id=row.alert_id,
            transaction_id=row.transaction_id,
            subject_wallet_id=row.subject_wallet_id,
            raised_at=row.raised_at,
            assessment=_assessment(row),
        )
        for row in connection.execute(query)
    ]
