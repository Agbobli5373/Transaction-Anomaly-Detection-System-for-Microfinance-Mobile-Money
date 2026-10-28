"""Postgres backups: timestamped `pg_dump` files, pruned to a retention count, and their restore.

A backup is one custom-format dump, which `pg_restore` can load into an empty database. The
connection comes from the same URL the rest of the system uses; its password, if any, is handed to
`pg_dump` and `pg_restore` through `PGPASSWORD`, never on a command line.
"""

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

# Backup file names sort by time, so the newest N are the last N in name order.
_STAMP = "%Y%m%dT%H%M%SZ"
_BACKUP = re.compile(r"^txrisk-\d{8}T\d{6}Z\.dump$")


class BackupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: Path
    keep: int = Field(ge=1)


def load_backup_config(path: Path) -> BackupConfig:
    with path.open("rb") as f:
        return BackupConfig.model_validate(tomllib.load(f))


class BackupError(RuntimeError):
    """A `pg_dump` or `pg_restore` run failed, or a restore target is not empty."""


def _run(tool: str, url: sa.URL, *args: str) -> None:
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    command = [tool, "--host", url.host or "localhost", "--dbname", str(url.database)]
    if url.port:
        command += ["--port", str(url.port)]
    if url.username:
        command += ["--username", url.username]
    result = subprocess.run([*command, *args], env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"{tool} failed: {result.stderr.strip()}")


def backup(database_url: str, directory: Path, *, keep: int, now: datetime | None = None) -> Path:
    """Dump the database into `directory`, prune to the `keep` newest backups, return the new file.

    `now` only names the file. The timestamp is operational metadata about when the dump ran, not
    business logic, so reading the wall clock here is outside ADR 0005.
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(_STAMP)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"txrisk-{stamp}.dump"
    partial = target.with_suffix(".partial")
    try:
        # Written aside and renamed, so a failed dump is never mistaken for the latest backup.
        _run(
            "pg_dump",
            sa.make_url(database_url),
            "--format=custom",
            "--no-owner",
            "--file",
            str(partial),
        )
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    prune(directory, keep=keep)
    return target


def backups(directory: Path) -> list[Path]:
    """The backups in a directory, oldest first. Other files there are left alone."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and _BACKUP.match(p.name))


def prune(directory: Path, *, keep: int) -> list[Path]:
    """Delete all but the `keep` newest backups and return the deleted files."""
    if keep < 1:
        raise ValueError("keep must be at least 1")
    stale = backups(directory)[:-keep]
    for path in stale:
        path.unlink()
    return stale


def latest_backup(directory: Path) -> Path | None:
    found = backups(directory)
    return found[-1] if found else None


def restore(backup_file: Path, database_url: str) -> None:
    """Load a backup into an existing, empty database; refuse one that already has tables."""
    url = sa.make_url(database_url).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            tables = sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
    if tables:
        raise BackupError(f"refusing to restore into a database that already has tables: {tables}")
    _run("pg_restore", url, "--no-owner", "--exit-on-error", str(backup_file))


@dataclass(frozen=True)
class RestoreCheck:
    """What a restored database holds, against its source."""

    source_counts: dict[str, int]
    restored_counts: dict[str, int]
    source_migration: str | None
    restored_migration: str | None

    @property
    def ok(self) -> bool:
        return (
            bool(self.source_counts)
            and self.source_counts == self.restored_counts
            and self.source_migration is not None
            and self.source_migration == self.restored_migration
        )


def _snapshot(engine: sa.Engine) -> tuple[dict[str, int], str | None]:
    with engine.connect() as connection:
        tables = sorted(sa.inspect(connection).get_table_names())
        quote = connection.dialect.identifier_preparer.quote
        counts = {
            table: int(
                connection.execute(sa.text(f"SELECT count(*) FROM {quote(table)}")).scalar_one()
            )
            for table in tables
        }
        latest = (
            connection.execute(sa.text("SELECT max(version) FROM schema_migrations")).scalar_one()
            if "schema_migrations" in tables
            else None
        )
    return counts, latest


def check_restore(source: sa.Engine, restored: sa.Engine) -> RestoreCheck:
    """Row counts per table and the latest recorded migration, for both databases."""
    source_counts, source_migration = _snapshot(source)
    restored_counts, restored_migration = _snapshot(restored)
    return RestoreCheck(source_counts, restored_counts, source_migration, restored_migration)
