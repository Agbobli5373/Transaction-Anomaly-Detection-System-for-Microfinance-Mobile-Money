"""The Postgres tables as SQLAlchemy Core sees them, to query with.

The migrations in `migrations/` define the schema; these definitions only describe it, and a test
checks the two agree. Money is numeric(14, 2) in Postgres and read back as float, which is exact
for amounts with at most two decimals, so served features equal offline ones.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

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

# An assessment that re-scoring replaced; the same columns, and the version that replaced it.
risk_assessment_history = sa.Table(
    "risk_assessment_history",
    metadata,
    sa.Column("history_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("transaction_id", sa.Text, nullable=False),
    sa.Column("risk_score", sa.Double, nullable=False),
    sa.Column("risk_band", sa.Text, nullable=False),
    sa.Column("reasons", JSONB, nullable=False),
    sa.Column("component_scores", JSONB, nullable=False),
    sa.Column("model_version", sa.Text, nullable=False),
    sa.Column("degraded", sa.Boolean, nullable=False),
    sa.Column("assessed_at", _TIME, nullable=False),
    sa.Column("replaced_by", sa.Text, nullable=False),
)

alerts = sa.Table(
    "alerts",
    metadata,
    sa.Column("alert_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("transaction_id", sa.Text, nullable=False, unique=True),
    sa.Column("subject_wallet_id", sa.Text, nullable=False),
    sa.Column("raised_at", _TIME, nullable=False),
    sa.Column("case_id", sa.BigInteger, sa.ForeignKey("cases.case_id"), nullable=False),
)

cases = sa.Table(
    "cases",
    metadata,
    sa.Column("case_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("subject_wallet_id", sa.Text, sa.ForeignKey("wallets.wallet_id"), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("opened_at", _TIME, nullable=False),
    sa.Column("assignee_id", sa.Text),
    sa.Column("assignee_name", sa.Text),
    sa.Column("assigned_by", sa.Text),
    sa.Column("closed_at", _TIME),
    sa.Column("closed_by_id", sa.Text),
    sa.Column("closed_by_name", sa.Text),
)

case_events = sa.Table(
    "case_events",
    metadata,
    sa.Column("event_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("case_id", sa.BigInteger, sa.ForeignKey("cases.case_id"), nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("occurred_at", _TIME, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("actor_name", sa.Text, nullable=False),
    sa.Column("body", sa.Text),
    sa.Column("from_status", sa.Text),
    sa.Column("to_status", sa.Text),
    sa.Column("assignee_id", sa.Text),
)

outbox = sa.Table(
    "outbox",
    metadata,
    sa.Column("outbox_id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("topic", sa.Text, nullable=False),
    sa.Column("key", sa.Text, nullable=False),
    sa.Column("payload", sa.Text, nullable=False),
    sa.Column("published", sa.Boolean, nullable=False),
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

api_keys = sa.Table(
    "api_keys",
    metadata,
    sa.Column("key_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("key_digest", sa.LargeBinary, nullable=False),
    sa.Column("scopes", ARRAY(sa.Text), nullable=False),
    sa.Column("rate_limit_per_minute", sa.Integer, nullable=False),
    sa.Column("created_at", _TIME, nullable=False),
    sa.Column("created_by", sa.Text, nullable=False),
    sa.Column("expires_at", _TIME),
    sa.Column("revoked_at", _TIME),
    sa.Column("replaces", sa.Text),
)

# Append-only: the application's role may only select from it and insert into it.
audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("audit_id", sa.BigInteger, primary_key=True),
    sa.Column("occurred_at", _TIME, nullable=False),
    sa.Column("actor_type", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Text, nullable=False),
    sa.Column("actor_name", sa.Text, nullable=False),
    sa.Column("actor_roles", ARRAY(sa.Text), nullable=False),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("object_type", sa.Text),
    sa.Column("object_id", sa.Text),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("detail", JSONB, nullable=False),
)
