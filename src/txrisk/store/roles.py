"""The least-privilege database role the API and workers run as.

Migrations and the other jobs (seed, backup) connect as the owner, which can change anything. The
API and the scoring workers connect as `txrisk_app`, which holds only the rights listed in
`GRANTS`: in particular it can SELECT from and INSERT into `audit_log` and nothing else, owns no
table (so it cannot alter or drop one) and cannot create objects. That, with the triggers in
migration 0003, is what makes the audit log append-only to the application.

The migration job calls `provision_app_role` after applying migrations. Its password is the value
of `TXRISK_APP_DB_PASSWORD`, read from the environment (`.env` in compose) and never committed; a
re-run applies a changed value and resets the grants to exactly `GRANTS`.
"""

import os
from collections.abc import Mapping

import psycopg
import sqlalchemy as sa
from psycopg import sql

APP_ROLE = "txrisk_app"
PASSWORD_ENV = "TXRISK_APP_DB_PASSWORD"

# Every table's privileges for the application role. A table missing here makes provisioning
# fail, so a new migration cannot leave the application with rights nobody decided on. The
# application never deletes: the retention purge runs as the owner, as a job (see `txrisk purge`).
GRANTS: dict[str, tuple[str, ...]] = {
    "wallets": ("SELECT", "INSERT"),
    "agents": ("SELECT", "INSERT"),
    "merchants": ("SELECT", "INSERT"),
    "billers": ("SELECT", "INSERT"),
    "sim_swaps": ("SELECT", "INSERT"),
    "transactions": ("SELECT", "INSERT"),
    # UPDATE is for re-scoring after Degraded mode (the API and workers do it once the model
    # recovers); a trigger (migration 0005) refuses any update but replacing a degraded assessment.
    "risk_assessments": ("SELECT", "INSERT", "UPDATE"),
    # Replaced assessments are kept here, append-only.
    "risk_assessment_history": ("SELECT", "INSERT"),
    "alerts": ("SELECT", "INSERT"),
    # Keys are revoked and rotated (expiry set) in place, never deleted.
    "api_keys": ("SELECT", "INSERT", "UPDATE"),
    # The application checks that migrations are applied; only the migration job writes them.
    "schema_migrations": ("SELECT",),
    # Fraud labels are inserted when a discovery is consumed, and never changed (the purge deletes
    # them as the owner).
    "fraud_labels": ("SELECT", "INSERT"),
    # One row that workers and the API move forward with a conditional UPDATE.
    "simulation_clock": ("SELECT", "UPDATE"),
    # The scheduler records what it fired: it inserts a row per job, then updates it.
    "scheduled_jobs": ("SELECT", "INSERT", "UPDATE"),
    # A Case moves through its states, and is assigned, in place; nothing is ever deleted.
    "cases": ("SELECT", "INSERT", "UPDATE"),
    "case_events": ("SELECT", "INSERT"),
    # Events awaiting publication: inserted with the change, marked published by the publisher.
    "outbox": ("SELECT", "INSERT", "UPDATE"),
    # Append-only: no UPDATE, DELETE, TRUNCATE. Aged entries leave only through audit_log_purge,
    # which the owner runs (see migration 0004).
    "audit_log": ("SELECT", "INSERT"),
}


def provision_app_role(engine: sa.Engine, password: str) -> None:
    """Create `txrisk_app` (or set its password) and give it exactly `GRANTS`, as the owner.

    The password is hashed (SCRAM) by libpq before it is sent, so the statement never carries it
    in the clear.
    """
    with engine.begin() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        undecided = sorted(tables - GRANTS.keys())
        if undecided:
            raise RuntimeError(f"no grants decided for table(s) {', '.join(undecided)}; see GRANTS")
        raw = connection.connection.driver_connection
        assert isinstance(raw, psycopg.Connection)
        hashed = raw.pgconn.encrypt_password(password.encode(), APP_ROLE.encode()).decode()
        role = sql.Identifier(APP_ROLE)
        exists = connection.execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": APP_ROLE}
        ).first()
        options = sql.SQL(
            "LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        ).format(sql.Literal(hashed))
        verb = "ALTER" if exists else "CREATE"
        statements = [sql.SQL("{} ROLE {} {}").format(sql.SQL(verb), role, options)]
        database = connection.execute(sa.text("SELECT current_database()")).scalar_one()
        statements += [
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), role),
            sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}").format(role),
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
        ]
        for table in sorted(tables):
            privileges = sql.SQL(", ").join(sql.SQL(p) for p in GRANTS[table])
            statements.append(
                sql.SQL("GRANT {} ON {} TO {}").format(privileges, sql.Identifier(table), role)
            )
        cursor = raw.cursor()
        for statement in statements:
            cursor.execute(statement)


def app_role_password(environ: Mapping[str, str] = os.environ) -> str | None:
    """The role's password from the environment; None if unset or empty."""
    return environ.get(PASSWORD_ENV) or None
