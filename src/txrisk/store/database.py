"""Connecting to Postgres, and applying the schema migrations.

Migrations are plain SQL files applied in name order by a dedicated job (`txrisk migrate`); the
API and workers only check that the schema is current, and never change it.
"""

from importlib import resources

import sqlalchemy as sa

# Serialises concurrent migration jobs; any constant unique to this application will do.
_MIGRATION_LOCK = 7_281_245_001
_VERSIONS = sa.table("schema_migrations", sa.column("version", sa.Text))


def create_engine(url: str) -> sa.Engine:
    """An engine for a `postgresql://` URL, on psycopg, with every session in UTC.

    The features read the hour of day from timestamps, so they must come back in UTC.
    """
    if url.startswith(("postgresql://", "postgres://")):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    return sa.create_engine(url, connect_args={"options": "-c timezone=UTC"}, pool_pre_ping=True)


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
