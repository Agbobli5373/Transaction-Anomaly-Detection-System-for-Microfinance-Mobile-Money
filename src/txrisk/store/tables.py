"""The Postgres tables as SQLAlchemy Core sees them, to query with.

The migrations in `migrations/` define the schema; these definitions only describe it, and a test
checks the two agree. Money is numeric(14, 2) in Postgres and read back as float, which is exact
for amounts with at most two decimals, so served features equal offline ones.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

_MONEY = sa.Numeric(14, 2, asdecimal=False)
_TIME = sa.DateTime(timezone=True)

wallets = sa.Table(
    "wallets",
    metadata,
    sa.Column("wallet_id", sa.Text, primary_key=True),
    sa.Column("kyc_tier", sa.Text, nullable=False),
    sa.Column("home_region", sa.Text, nullable=False),
    sa.Column("registered_at", _TIME, nullable=False),
)

agents = sa.Table(
    "agents",
    metadata,
    sa.Column("agent_id", sa.Text, primary_key=True),
    sa.Column("region", sa.Text, nullable=False),
)

merchants = sa.Table(
    "merchants",
    metadata,
    sa.Column("merchant_id", sa.Text, primary_key=True),
    sa.Column("category", sa.Text, nullable=False),
    sa.Column("region", sa.Text, nullable=False),
)

billers = sa.Table(
    "billers",
    metadata,
    sa.Column("biller_id", sa.Text, primary_key=True),
    sa.Column("category", sa.Text, nullable=False),
)

sim_swaps = sa.Table(
    "sim_swaps",
    metadata,
    sa.Column("wallet_id", sa.Text, primary_key=True),
    sa.Column("occurred_at", _TIME, primary_key=True),
)

transactions = sa.Table(
    "transactions",
    metadata,
    sa.Column("transaction_id", sa.Text, primary_key=True),
    sa.Column("occurred_at", _TIME, nullable=False),
    sa.Column("transaction_type", sa.Text, nullable=False),
    sa.Column("amount", _MONEY, nullable=False),
    sa.Column("subject_wallet_id", sa.Text, nullable=False),
    sa.Column("direction", sa.Text, nullable=False),
    sa.Column("subject_balance_before", _MONEY, nullable=False),
    sa.Column("channel", sa.Text, nullable=False),
    sa.Column("device_id", sa.Text, nullable=False),
    sa.Column("region", sa.Text, nullable=False),
    sa.Column("counterparty_wallet_id", sa.Text),
    sa.Column("agent_id", sa.Text),
    sa.Column("merchant_id", sa.Text),
    sa.Column("biller_id", sa.Text),
)

risk_assessments = sa.Table(
    "risk_assessments",
    metadata,
    sa.Column("transaction_id", sa.Text, primary_key=True),
    sa.Column("risk_score", sa.Double, nullable=False),
    sa.Column("risk_band", sa.Text, nullable=False),
    sa.Column("reasons", JSONB, nullable=False),
    sa.Column("component_scores", JSONB, nullable=False),
    sa.Column("model_version", sa.Text, nullable=False),
    sa.Column("degraded", sa.Boolean, nullable=False),
    sa.Column("assessed_at", _TIME, nullable=False),
)

alerts = sa.Table(
    "alerts",
    metadata,
    sa.Column("alert_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("transaction_id", sa.Text, nullable=False, unique=True),
    sa.Column("subject_wallet_id", sa.Text, nullable=False),
    sa.Column("raised_at", _TIME, nullable=False),
)

# The column holding each Transaction type's Counterparty.
COUNTERPARTY_COLUMNS = {
    "cash_in": "agent_id",
    "cash_out": "agent_id",
    "transfer": "counterparty_wallet_id",
    "payment": "merchant_id",
    "bill_payment": "biller_id",
}

fraud_labels = sa.Table(
    "fraud_labels",
    metadata,
    sa.Column("transaction_id", sa.Text, primary_key=True),
    sa.Column("discovered_at", _TIME, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
)

simulation_clock = sa.Table(
    "simulation_clock",
    metadata,
    sa.Column("singleton", sa.Boolean, primary_key=True),
    sa.Column("at", _TIME),
)

scheduled_jobs = sa.Table(
    "scheduled_jobs",
    metadata,
    sa.Column("job_name", sa.Text, primary_key=True),
    sa.Column("period", sa.Date),
    sa.Column("fired_at", _TIME),
    sa.Column("launch_ref", sa.Text),
)
