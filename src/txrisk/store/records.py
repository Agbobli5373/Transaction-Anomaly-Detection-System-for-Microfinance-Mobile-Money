"""Reading and writing Transactions, parties, Risk assessments and Alerts."""

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, insert

from txrisk.features import Transaction
from txrisk.scoring import Reason, RiskAssessment, RiskBand, ShadowScore
from txrisk.store.history import select_transactions, transaction_from_row
from txrisk.store.tables import (
    COUNTERPARTY_COLUMNS,
    agents,
    alerts,
    billers,
    cases,
    challenger_scores,
    merchants,
    model_inputs,
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


def any_of(column: sa.Column[Any], values: Iterable[str]) -> sa.ColumnElement[bool]:
    """`column = ANY (:values)`: one array parameter however many values there are, which the
    driver and the planner handle far more cheaply than an `IN` list of hundreds."""
    return column == sa.any_(sa.bindparam(None, sorted(values), type_=ARRAY(column.type)))


def _arrays(**columns: tuple[Any, Sequence[object]]) -> list[sa.BindParameter[Any]]:
    """One array parameter for each column of a bulk insert, by its type and values."""
    return [
        sa.bindparam(name, list(values), type_=ARRAY(kind))
        for name, (kind, values) in columns.items()
    ]


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


def save_model_inputs(
    connection: sa.Connection, assessment: RiskAssessment, *, assessed_at: datetime
) -> None:
    """Keep the model inputs the assessment was scored with, for drift monitoring; nothing for
    an assessment that was not made by a model. An input that was empty is stored as null."""
    if assessment.model_inputs is None:
        return
    connection.execute(
        insert(model_inputs)
        .values(
            transaction_id=assessment.transaction_id,
            assessed_at=assessed_at,
            model_version=assessment.model_version,
            inputs={k: _json_number(v) for k, v in assessment.model_inputs.items()},
        )
        .on_conflict_do_update(
            index_elements=[model_inputs.c.transaction_id],
            set_={
                "model_version": assessment.model_version,
                "inputs": {k: _json_number(v) for k, v in assessment.model_inputs.items()},
            },
        )
    )


def save_challenger_score(
    connection: sa.Connection, shadow: ShadowScore, *, transaction_id: str, assessed_at: datetime
) -> bool:
    """Keep what a Challenger model scored a Transaction, apart from the Champion's assessment;
    False, storing nothing, if that model already scored it. Never an Alert: only the Champion's
    High band raises one."""
    inserted = connection.execute(
        insert(challenger_scores)
        .values(
            transaction_id=transaction_id,
            model_version=shadow.model_version,
            risk_score=shadow.risk_score,
            risk_band=None if shadow.risk_band is None else shadow.risk_band.value,
            component_scores=None
            if shadow.error is not None
            else {k: _json_number(v) for k, v in shadow.component_scores.items()},
            error=shadow.error,
            latency_ms=shadow.latency_ms,
            assessed_at=assessed_at,
        )
        .on_conflict_do_nothing()
        .returning(challenger_scores.c.transaction_id)
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


def assessment_from_row(row: sa.Row[Any]) -> RiskAssessment:
    return _assessment(
        row.transaction_id,
        row.risk_score,
        row.risk_band,
        row.reasons,
        row.component_scores,
        row.model_version,
        row.degraded,
    )


def _assessment(
    transaction_id: str,
    risk_score: float,
    risk_band: str,
    reasons: list[dict[str, Any]],
    component_scores: dict[str, float | None],
    model_version: str,
    degraded: bool,
) -> RiskAssessment:
    return RiskAssessment(
        transaction_id=transaction_id,
        risk_score=risk_score,
        risk_band=RiskBand(risk_band),
        reasons=tuple(Reason(r["code"], r["message"], r["value"]) for r in reasons),
        component_scores=dict(component_scores),
        model_version=model_version,
        degraded=degraded,
    )


@dataclass(frozen=True)
class Lookup:
    """What storage already knows about a Transaction about to be scored."""

    # The stored assessment, if the Transaction was scored.
    assessment: RiskAssessment | None
    # The stored Transaction under its ID, if there is one (an assessment implies it).
    transaction: Transaction | None
    # The parties it names that are not registered, as `column=value`.
    missing_parties: list[str]


def lookup(connection: sa.Connection, transaction: Transaction) -> Lookup:
    """The stored assessment and Transaction under this Transaction's ID, and which of its
    parties are unregistered, in one round trip."""
    column = COUNTERPARTY_COLUMNS[transaction.transaction_type]
    a = risk_assessments.c
    asked = sa.select(sa.literal(transaction.transaction_id, sa.Text).label("id")).subquery("asked")
    row = connection.execute(
        sa.select(
            a.risk_score,
            a.risk_band,
            a.reasons,
            a.component_scores,
            a.model_version,
            a.degraded,
            *select_transactions().selected_columns,
            sa.exists().where(wallets.c.wallet_id == transaction.subject_wallet_id),
            sa.exists().where(_PARTY_KEYS[column] == transaction.counterparty_id),
        ).select_from(
            asked.outerjoin(transactions, transactions.c.transaction_id == asked.c.id).outerjoin(
                risk_assessments, risk_assessments.c.transaction_id == asked.c.id
            )
        )
    ).one()
    known = row[-2:]
    missing = [
        f"{name}={value}"
        for name, value, found in (
            ("subject_wallet_id", transaction.subject_wallet_id, known[0]),
            (column, transaction.counterparty_id, known[1]),
        )
        if not found
    ]
    stored = row[6] is not None
    return Lookup(
        assessment=None
        if row.risk_band is None
        else _assessment(transaction.transaction_id, *row[:6]),
        transaction=transaction_from_row(row[6:-2]) if stored else None,
        missing_parties=missing,
    )


def stored_many(
    connection: sa.Connection, transaction_ids: Sequence[str]
) -> dict[str, tuple[Transaction, RiskAssessment | None]]:
    """The stored Transaction of each of these IDs that has one, with its assessment if it was
    scored, in one round trip."""
    a = risk_assessments.c
    found = {}
    for row in connection.execute(
        sa.select(
            *select_transactions().selected_columns,
            a.risk_score,
            a.risk_band,
            a.reasons,
            a.component_scores,
            a.model_version,
            a.degraded,
        )
        .select_from(
            transactions.outerjoin(
                risk_assessments, risk_assessments.c.transaction_id == transactions.c.transaction_id
            )
        )
        .where(any_of(transactions.c.transaction_id, transaction_ids))
    ):
        stored = transaction_from_row(row[:11])
        found[stored.transaction_id] = (
            stored,
            None if row[12] is None else _assessment(stored.transaction_id, *row[11:]),
        )
    return found


def unregistered(connection: sa.Connection, batch: Sequence[Transaction]) -> dict[str, list[str]]:
    """For each Transaction of the batch that names a party that is not registered, what it
    names that is not, as `column=value`; one query for each kind of party."""
    wanted: dict[str, set[str]] = {"wallet_id": {t.subject_wallet_id for t in batch}}
    for t in batch:
        column = COUNTERPARTY_COLUMNS[t.transaction_type]
        wanted.setdefault(_PARTY_KEYS[column].key, set()).add(t.counterparty_id)
    columns = {column.key: column for column in _PARTY_KEYS.values()}
    registered = {
        (key, value)
        for key, ids in wanted.items()
        for value in connection.execute(
            sa.select(columns[key]).where(any_of(columns[key], ids))
        ).scalars()
    }
    missing: dict[str, list[str]] = {}
    for t in batch:
        column = COUNTERPARTY_COLUMNS[t.transaction_type]
        named = [
            ("subject_wallet_id", "wallet_id", t.subject_wallet_id),
            (column, _PARTY_KEYS[column].key, t.counterparty_id),
        ]
        absent = [f"{name}={value}" for name, key, value in named if (key, value) not in registered]
        if absent:
            missing[t.transaction_id] = absent
    return missing


_TRANSACTION_INSERT = """
INSERT INTO transactions (
    transaction_id, occurred_at, transaction_type, amount, subject_wallet_id, direction,
    subject_balance_before, channel, device_id, region,
    counterparty_wallet_id, agent_id, merchant_id, biller_id
)
SELECT * FROM unnest(
    :transaction_id, :occurred_at, :transaction_type, :amount, :subject_wallet_id, :direction,
    :subject_balance_before, :channel, :device_id, :region,
    :counterparty_wallet_id, :agent_id, :merchant_id, :biller_id
)
ON CONFLICT DO NOTHING
RETURNING transaction_id
"""


def record_transactions(connection: sa.Connection, batch: Sequence[Transaction]) -> set[str]:
    """Store the Transactions, in one statement; the IDs of those stored now, so not those that
    already were (stored under the same ID by an earlier or concurrent call)."""
    if not batch:
        return set()
    # Every row names every Counterparty column, the ones its type does not use empty.
    rows = [{**dict.fromkeys(_PARTY_KEYS), **_transaction_values(t)} for t in batch]
    column = transactions.c

    def values(name: str) -> tuple[Any, list[object]]:
        return column[name].type, [row[name] for row in rows]

    statement = sa.text(_TRANSACTION_INSERT).bindparams(
        *_arrays(**{c.name: values(c.name) for c in transactions.columns})
    )
    return set(connection.execute(statement).scalars())


_ASSESSMENT_INSERT = """
INSERT INTO risk_assessments (
    transaction_id, risk_score, risk_band, reasons, component_scores, model_version, degraded,
    assessed_at
)
SELECT transaction_id, risk_score, risk_band, CAST(reasons AS jsonb),
       CAST(component_scores AS jsonb), model_version, degraded, assessed_at
FROM unnest(
    :transaction_id, :risk_score, :risk_band, :reasons, :component_scores, :model_version,
    :degraded, :assessed_at
) AS v(
    transaction_id, risk_score, risk_band, reasons, component_scores, model_version, degraded,
    assessed_at
)
ON CONFLICT DO NOTHING
RETURNING *
"""


def save_assessments_returning(
    connection: sa.Connection, saved: Sequence[tuple[RiskAssessment, datetime]]
) -> dict[str, RiskAssessment]:
    """Store the assessments, each with its `assessed_at`, in one statement, and return those
    stored now as stored; an assessment that already has one stored is not in the result."""
    if not saved:
        return {}
    values = [{**_assessment_values(a), "transaction_id": a.transaction_id} for a, _ in saved]
    statement = sa.text(_ASSESSMENT_INSERT).bindparams(
        *_arrays(
            transaction_id=(sa.Text, [v["transaction_id"] for v in values]),
            risk_score=(sa.Double, [v["risk_score"] for v in values]),
            risk_band=(sa.Text, [v["risk_band"] for v in values]),
            reasons=(sa.Text, [json.dumps(v["reasons"]) for v in values]),
            component_scores=(sa.Text, [json.dumps(v["component_scores"]) for v in values]),
            model_version=(sa.Text, [v["model_version"] for v in values]),
            degraded=(sa.Boolean, [v["degraded"] for v in values]),
            assessed_at=(sa.DateTime(timezone=True), [assessed_at for _, assessed_at in saved]),
        )
    )
    return {row.transaction_id: assessment_from_row(row) for row in connection.execute(statement)}


_MODEL_INPUTS_UPSERT = """
INSERT INTO model_inputs (transaction_id, assessed_at, model_version, inputs)
SELECT transaction_id, assessed_at, model_version, CAST(inputs AS jsonb)
FROM unnest(:transaction_id, :assessed_at, :model_version, :inputs)
    AS v(transaction_id, assessed_at, model_version, inputs)
ON CONFLICT (transaction_id) DO UPDATE
    SET model_version = EXCLUDED.model_version, inputs = EXCLUDED.inputs
"""


def save_model_inputs_many(
    connection: sa.Connection, saved: Sequence[tuple[RiskAssessment, datetime]]
) -> None:
    """`save_model_inputs` of each assessment that has model inputs, in one statement."""
    held = [(a, at) for a, at in saved if a.model_inputs is not None]
    if not held:
        return
    connection.execute(
        sa.text(_MODEL_INPUTS_UPSERT).bindparams(
            *_arrays(
                transaction_id=(sa.Text, [a.transaction_id for a, _ in held]),
                assessed_at=(sa.DateTime(timezone=True), [at for _, at in held]),
                model_version=(sa.Text, [a.model_version for a, _ in held]),
                inputs=(
                    sa.Text,
                    [
                        json.dumps({k: _json_number(v) for k, v in a.model_inputs.items()})
                        for a, _ in held
                        if a.model_inputs is not None
                    ],
                ),
            )
        )
    )


def save_assessment_returning(
    connection: sa.Connection, assessment: RiskAssessment, *, assessed_at: datetime
) -> RiskAssessment | None:
    """Store the assessment and return it as stored (what a replay will read back); None,
    storing nothing, if one is already stored for it."""
    row = connection.execute(
        insert(risk_assessments)
        .values(
            transaction_id=assessment.transaction_id,
            **_assessment_values(assessment),
            assessed_at=assessed_at,
        )
        .on_conflict_do_nothing()
        .returning(*risk_assessments.c)
    ).first()
    return None if row is None else assessment_from_row(row)


def stored_assessment(connection: sa.Connection, transaction_id: str) -> RiskAssessment | None:
    row = connection.execute(
        sa.select(risk_assessments).where(risk_assessments.c.transaction_id == transaction_id)
    ).one_or_none()
    return None if row is None else assessment_from_row(row)


# The statuses of a Case that is still being worked: an Alert joins such a Case.
_ACTIVE = ("open", "investigating")


def _join_or_open_case(connection: sa.Connection, wallet_id: str, opened_at: datetime) -> int:
    """The Subject wallet's active Case, opening one if it has none.

    One active Case per wallet is a unique partial index, so concurrent Alerts for a wallet with
    none cannot each open one: the loser's insert does nothing and it joins the winner's. A found
    Case is locked FOR SHARE, so a Supervisor or Analyst closing it at this moment waits for this
    Alert to commit and the closing then sees it; a Case closed first is simply not found.
    """
    while True:
        found: int | None = connection.execute(
            sa.select(cases.c.case_id)
            .where(cases.c.subject_wallet_id == wallet_id, cases.c.status.in_(_ACTIVE))
            .with_for_update(read=True)
        ).scalar_one_or_none()
        if found is not None:
            return found
        opened: int | None = connection.execute(
            insert(cases)
            .values(subject_wallet_id=wallet_id, status="open", opened_at=opened_at)
            .on_conflict_do_nothing()
            .returning(cases.c.case_id)
        ).scalar_one_or_none()
        if opened is not None:
            return opened


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
    return [assessment_from_row(row) for row in rows]


def raise_alert(connection: sa.Connection, transaction: Transaction) -> bool:
    """Queue an Alert for the Transaction, raised at its event time, in its Subject wallet's
    active Case (opening one if there is none); False if it has an Alert already."""
    exists = connection.execute(
        sa.select(sa.literal(1)).where(alerts.c.transaction_id == transaction.transaction_id)
    ).first()
    if exists is not None:
        return False
    case_id = _join_or_open_case(connection, transaction.subject_wallet_id, transaction.occurred_at)
    inserted = connection.execute(
        insert(alerts)
        .values(
            transaction_id=transaction.transaction_id,
            subject_wallet_id=transaction.subject_wallet_id,
            raised_at=transaction.occurred_at,
            case_id=case_id,
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
            assessment=assessment_from_row(row),
        )
        for row in connection.execute(query)
    ]
