"""Connecting to Postgres, and applying the schema migrations.

Migrations are plain SQL files applied in name order by a dedicated job (`txrisk migrate`); the
API and workers only check that the schema is current, and never change it.
"""

import os
from collections.abc import Mapping
from importlib import resources

import sqlalchemy as sa

from txrisk.store.roles import APP_ROLE, app_role_password

# Serialises concurrent migration jobs; any constant unique to this application will do.
_MIGRATION_LOCK = 7_281_245_001
_VERSIONS = sa.table("schema_migrations", sa.column("version", sa.Text))


def create_engine(url: str | sa.URL) -> sa.Engine:
    """An engine for a `postgresql://` URL, on psycopg, with every session in UTC.

    The features read the hour of day from timestamps, so they must come back in UTC.
    """
    parsed = sa.make_url(url)
    if parsed.drivername in ("postgresql", "postgres"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return sa.create_engine(parsed, connect_args={"options": "-c timezone=UTC"}, pool_pre_ping=True)


def create_app_engine(url: str, environ: Mapping[str, str] = os.environ) -> sa.Engine:
    """The engine the API and workers use. When `url` names the application role without a
    credential, its password comes from the environment (`TXRISK_APP_DB_PASSWORD`), not from the
    URL, so no credential is written in configuration; libpq's own `PGPASSWORD` stays the owner's,
    for the migration job and the other jobs."""
    parsed = sa.make_url(url)
    supplied = app_role_password(environ)
    if parsed.username == APP_ROLE and parsed.password is None and supplied is not None:
        parsed = parsed.set(password=supplied)
    return create_engine(parsed)


def migrations() -> dict[str, str]:
    """Every migration's SQL by version (its file name without `.sql`), in the order to apply."""
    folder = resources.files("txrisk.store") / "migrations"
    found = {
        entry.name.removesuffix(".sql"): entry.read_text(encoding="utf-8")
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    }
    return dict(sorted(found.items()))


def applied_migrations(connection: sa.Connection) -> set[str]:
    """The versions already applied; empty for a database never migrated."""
    if not sa.inspect(connection).has_table("schema_migrations"):
        return set()
    return set(connection.execute(sa.select(_VERSIONS.c.version)).scalars())


def pending_migrations(connection: sa.Connection) -> list[str]:
    applied = applied_migrations(connection)
    return [version for version in migrations() if version not in applied]


def migrate(engine: sa.Engine) -> list[str]:
    """Apply every pending migration, all in one transaction, and return their versions.

    Postgres DDL is transactional, so a failed migration leaves the schema as it was.
    """
    with engine.begin() as connection:
        connection.execute(sa.select(sa.func.pg_advisory_xact_lock(_MIGRATION_LOCK)))
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version text PRIMARY KEY,"
                " applied_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        pending = pending_migrations(connection)
        sql = migrations()
        cursor = connection.connection.cursor()
        for version in pending:
            # Without parameters psycopg runs the whole file, several statements at once.
            cursor.execute(sql[version])
            connection.execute(sa.insert(_VERSIONS).values(version=version))
        return pending
