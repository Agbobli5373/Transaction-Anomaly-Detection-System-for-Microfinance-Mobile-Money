"""Demo Cases: a few realistic Cases and Medium-band Transactions, for the workbench's end-to-end
tests and for trying it by hand.

They are made the way scoring makes them, through the store's own functions (a Wallet, its
routine Transactions, a High-band Risk assessment with its Reasons, the Alert and the Case it
joins), so the API and the workbench see nothing a real Case would not have. It runs as the
database owner against a migrated database (`txrisk seed-cases`); the API has no way to create
Cases and gets none for this. Everything is fixed or derived from `tag`, never the wall clock
(ADR 0005), and running it again with the same tag changes nothing.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from txrisk.features import Transaction
from txrisk.scoring import Reason, RiskAssessment, RiskBand
from txrisk.store import cases, records

# When the first demo Transaction happens; later ones follow it.
BASE = datetime(2030, 6, 1, 14, 0, tzinfo=UTC)
REGISTERED = datetime(2030, 1, 1, tzinfo=UTC)
REGIONS = ("Greater Accra", "Ashanti", "Northern", "Volta")
MEDIUM_TYPES = ("transfer", "cash_out", "cash_in")

_REASONS = (
    Reason("SIM_SWAP_DRAIN", "Balance drained shortly after a SIM swap", 0.9),
    Reason("NEW_DEVICE", "First use of this device on the Wallet", 0.0),
    Reason("AMOUNT_VS_USUAL", "Amount well above the Wallet's usual", 18.5),
)


@dataclass(frozen=True)
class DemoCase:
    case_id: int
    wallet_id: str


def _assessment(transaction: Transaction, band: RiskBand, score: float) -> RiskAssessment:
    reasons = {RiskBand.HIGH: _REASONS, RiskBand.MEDIUM: _REASONS[:1], RiskBand.LOW: ()}[band]
    return RiskAssessment(
        transaction_id=transaction.transaction_id,
        risk_score=score,
        risk_band=band,
        reasons=reasons,
        component_scores={"rules": score / 100, "anomaly": None, "supervised": None},
    )


def _transaction(
    transaction_id: str,
    at: datetime,
    wallet: str,
    *,
    kind: str,
    amount: float,
    counterparty: str,
    region: str,
    balance: float = 1000.0,
) -> Transaction:
    return Transaction(
        transaction_id=transaction_id,
        occurred_at=at,
        transaction_type=kind,
        amount=amount,
        subject_wallet_id=wallet,
        direction="in" if kind == "cash_in" else "out",
        counterparty_id=counterparty,
        region=region,
        subject_balance_before=balance,
        device_id=f"device-{wallet}",
        channel="app",
    )


def _store(
    connection: sa.Connection, transaction: Transaction, band: RiskBand, score: float
) -> bool:
    """Store a Transaction with its assessment; False if it was stored already."""
    if not records.record_transaction(connection, transaction):
        return False
    records.save_assessment(
        connection,
        _assessment(transaction, band, score),
        assessed_at=transaction.occurred_at,
    )
    return True


def _case_of(connection: sa.Connection, transaction_id: str) -> int:
    return int(
        connection.execute(
            sa.text("SELECT case_id FROM alerts WHERE transaction_id = :t"),
            {"t": transaction_id},
        ).scalar_one()
    )


def seed_demo_cases(
    connection: sa.Connection,
    *,
    tag: str,
    count: int,
    medium: int = 0,
    assignee: tuple[str, str] | None = None,
) -> list[DemoCase]:
    """`count` Cases, each on its own Wallet `w-<tag>-<n>` with four routine Transactions and
    a High Transaction that raised the Alert, plus `medium` Medium-band Transactions across the
    regions and Transaction types. With `assignee` (a subject ID and a name) every Case is
    assigned to that Analyst, by the seeding itself rather than a Supervisor."""
    friend = f"w-{tag}-friend"
    agent = f"A-{tag}"
    records.add_wallet(
        connection, friend, kyc_tier="tier2", home_region=REGIONS[0], registered_at=REGISTERED
    )
    records.add_agent(connection, agent, region=REGIONS[0])

    made: list[DemoCase] = []
    for n in range(count):
        wallet = f"w-{tag}-{n:03d}"
        region = REGIONS[n % len(REGIONS)]
        records.add_wallet(
            connection, wallet, kyc_tier="tier2", home_region=region, registered_at=REGISTERED
        )
        at = BASE + timedelta(hours=n)
        for day in range(4, 0, -1):
            routine = _transaction(
                f"T-{tag}-{n:03d}-r{day}",
                at - timedelta(days=day),
                wallet,
                kind="transfer",
                amount=50.0,
                counterparty=friend,
                region=region,
            )
            _store(connection, routine, RiskBand.LOW, 5.0)
        flagged = _transaction(
            f"T-{tag}-{n:03d}-hi",
            at,
            wallet,
            kind="transfer",
            amount=900.0 + n,
            counterparty=friend,
            region=region,
        )
        if _store(connection, flagged, RiskBand.HIGH, 80.0 + n % 15):
            records.raise_alert(connection, flagged)
        case_id = _case_of(connection, flagged.transaction_id)
        existing = cases.get_case(connection, case_id)
        if assignee is not None and existing is not None and existing.assignee_id is None:
            cases.assign(
                connection,
                case_id,
                assignee_id=assignee[0],
                assignee_name=assignee[1],
                actor_id="seed-cases",
                actor_name="Demo seeding",
            )
        made.append(DemoCase(case_id, wallet))

    for m in range(medium):
        wallet = f"w-{tag}-m{m % 3}"
        region = REGIONS[m % len(REGIONS)]
        records.add_wallet(
            connection, wallet, kyc_tier="tier1", home_region=region, registered_at=REGISTERED
        )
        kind = MEDIUM_TYPES[m % len(MEDIUM_TYPES)]
        _store(
            connection,
            _transaction(
                f"T-{tag}-med-{m:03d}",
                BASE + timedelta(days=1, minutes=m),
                wallet,
                kind=kind,
                amount=300.0 + m,
                counterparty=friend if kind == "transfer" else agent,
                region=region,
            ),
            RiskBand.MEDIUM,
            45.0 + m % 5,
        )
    return made
