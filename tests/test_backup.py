"""Postgres backups: pruning to a retention count, and a backup restored into an empty database."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from conftest import DEFAULT_CONFIG, create_database
from market_helpers import START, Market, build_market

from txrisk.cli import main
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.store import (
    BackupError,
    backup,
    check_restore,
    create_engine,
    latest_backup,
    load_backup_config,
    prune,
    restore,
    seed,
)
from txrisk.store.database import migrations

NOON = datetime(2026, 3, 1, 12, tzinfo=UTC)
BACKUP_CONFIG = DEFAULT_CONFIG.parent / "backup.toml"


def _fake_backups(directory: Path, count: int) -> list[Path]:
    """`count` backup files, one hour apart, oldest first."""
    names = [f"txrisk-{(NOON + timedelta(hours=i)):%Y%m%dT%H%M%SZ}.dump" for i in range(count)]
    for name in names:
        (directory / name).write_bytes(b"x")
    return [directory / name for name in names]


def test_pruning_keeps_exactly_the_newest_backups(tmp_path: Path):
    files = _fake_backups(tmp_path, 6)

    deleted = prune(tmp_path, keep=4)

    assert deleted == files[:2]
    assert sorted(tmp_path.iterdir()) == files[2:]
    assert latest_backup(tmp_path) == files[-1]


def test_pruning_with_fewer_backups_than_the_count_deletes_nothing(tmp_path: Path):
    files = _fake_backups(tmp_path, 2)

    assert prune(tmp_path, keep=5) == []
    assert sorted(tmp_path.iterdir()) == files


def test_pruning_leaves_files_that_are_not_backups(tmp_path: Path):
    _fake_backups(tmp_path, 3)
    (tmp_path / "notes.txt").write_text("keep me")

    prune(tmp_path, keep=1)

    assert (tmp_path / "notes.txt").exists()


def test_pruning_cannot_keep_nothing(tmp_path: Path):
    with pytest.raises(ValueError):
        prune(tmp_path, keep=0)


def test_the_shipped_backup_config_keeps_at_least_one_backup():
    assert load_backup_config(BACKUP_CONFIG).keep >= 1


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The market's parties and history on disk, without the offline-only files."""
    folder = tmp_path_factory.mktemp("generated")
    write_dataset(market.dataset, folder)
    for name in ("ground_truth", "fraud_discoveries", "benign_anomalies"):
        (folder / f"{name}.parquet").unlink()
    return folder


@pytest.fixture
def seeded(data: Path, database: str) -> str:
    until = pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=7)
    seed(create_engine(database), data, until=until.to_pydatetime())
    return database


def test_a_backup_restores_into_an_empty_database_with_the_same_contents(
    seeded: str, empty_database: str, tmp_path: Path
):
    written = backup(seeded, tmp_path, keep=3, now=NOON)

    restore(written, empty_database)

    check = check_restore(create_engine(seeded), create_engine(empty_database))
    assert check.ok
    assert check.restored_counts["transactions"] > 0
    assert check.restored_migration == list(migrations())[-1]


def test_restoring_refuses_a_database_that_already_has_tables(
    seeded: str, database: str, tmp_path: Path
):
    written = backup(seeded, tmp_path, keep=3, now=NOON)

    with pytest.raises(BackupError, match="already has tables"):
        restore(written, database)


def test_a_backup_is_named_by_its_time_and_prunes_the_old_ones(database: str, tmp_path: Path):
    old = _fake_backups(tmp_path, 3)

    written = backup(database, tmp_path, keep=2, now=NOON + timedelta(days=1))

    assert written.name == "txrisk-20260302T120000Z.dump"
    assert sorted(tmp_path.iterdir()) == [old[2], written]


def test_a_failed_backup_leaves_no_file_and_keeps_the_old_ones(postgres: str, tmp_path: Path):
    old = _fake_backups(tmp_path, 2)
    missing = sa.make_url(postgres).set(database="no_such_database")

    with pytest.raises(BackupError):
        backup(missing.render_as_string(hide_password=False), tmp_path, keep=1, now=NOON)

    assert sorted(tmp_path.iterdir()) == old


def test_the_cli_backs_up_and_restores_the_latest_backup_and_compares_it(
    seeded: str, empty_database: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    folder = tmp_path / "backups"

    assert main(["backup", "--database-url", seeded, "--out", str(folder), "--keep", "2"]) == 0
    assert latest_backup(folder) is not None
    exit_code = main(
        ["restore", str(folder), "--database-url", empty_database, "--compare-with", seeded]
    )

    assert exit_code == 0
    assert f"latest migration: {list(migrations())[-1]}" in capsys.readouterr().out


def test_the_cli_restore_fails_when_the_restore_differs_from_the_source(
    seeded: str, postgres: str, migrated_template: str, empty_database: str, tmp_path: Path
):
    # A migrated database with no rows cannot match the seeded source.
    unseeded = create_database(postgres, "unseeded", template=migrated_template)
    written = backup(seeded, tmp_path, keep=1, now=NOON)

    exit_code = main(
        ["restore", str(written), "--database-url", empty_database, "--compare-with", unseeded]
    )

    assert exit_code == 1
