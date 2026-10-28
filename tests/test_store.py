"""Postgres: migrations, seeding a generated dataset, and the history the features read from it."""

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest
import sqlalchemy as sa
from conftest import DEFAULT_CONFIG, create_database
from market_helpers import START, Market, build_market

from txrisk.cli import main
from txrisk.features import features_frame, transaction_features, transactions_from_frame
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.store import PostgresHistory, create_engine, migrate, pending_migrations, seed
from txrisk.store.database import migrations
from txrisk.store.tables import metadata

DAY = timedelta(days=1)
# What the running system may never see (ADR 0007): the generator writes these files, and no
# trace of them may reach Postgres.
OFFLINE_ONLY = ("ground_truth", "fraud_discoveries", "benign_anomalies")


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The market's dataset on disk, as `txrisk generate` writes it, without the offline-only
    files: seeding must not need them."""
    folder = tmp_path_factory.mktemp("generated")
    write_dataset(market.dataset, folder)
    for name in OFFLINE_ONLY:
        (folder / f"{name}.parquet").unlink()
    return folder


def _count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def test_migrate_applies_each_migration_once(empty_database: str):
    engine = create_engine(empty_database)

    first = migrate(engine)
    second = migrate(engine)

    assert first == list(migrations())
    assert first[:4] == [
        "0001_initial",
        "0002_api_keys",
        "0003_clock_scheduler_labels",
        "0004_audit_log",
    ]
    assert second == []
    with engine.connect() as connection:
        assert pending_migrations(connection) == []


def test_the_migrated_schema_has_the_columns_the_queries_use(database: str):
    inspector = sa.inspect(create_engine(database))

    for table in metadata.sorted_tables:
        migrated = [c["name"] for c in inspector.get_columns(table.name)]
        assert migrated == [c.name for c in table.columns], table.name


def test_txrisk_migrate_applies_the_schema(empty_database: str, capsys: pytest.CaptureFixture[str]):
    assert main(["migrate", "--database-url", empty_database]) == 0
    assert main(["migrate", "--database-url", empty_database]) == 0

    out = capsys.readouterr().out
    assert "applied 0001_initial" in out
    assert "already up to date" in out


def test_seed_loads_every_party_and_the_history_before_the_cutoff(
    market: Market, data: Path, database: str, capsys: pytest.CaptureFixture[str]
):
    exit_code = main(
        [
            "seed",
            "--data",
            str(data),
            "--database-url",
            database,
            "--config",
            str(DEFAULT_CONFIG),
            "--until-day",
            "14",
        ]
    )

    assert exit_code == 0
    assert "before day 14 (2026-01-15)" in capsys.readouterr().out
    cutoff = pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=14)
    engine = create_engine(database)
    before = market.transactions["occurred_at"] < cutoff
    assert _count(engine, "transactions") == before.sum() < len(market.transactions)
    assert _count(engine, "sim_swaps") == (market.sim_swaps["occurred_at"] < cutoff).sum()
    assert _count(engine, "wallets") == len(market.wallets)
    assert _count(engine, "agents") == len(market.dataset.agents)
    assert _count(engine, "merchants") == len(market.dataset.merchants)
    assert _count(engine, "billers") == len(market.dataset.billers)
    # History carries no assessments: nothing has been scored yet.
    assert _count(engine, "risk_assessments") == _count(engine, "alerts") == 0


def test_seeding_again_adds_only_what_is_missing(market: Market, data: Path, database: str):
    engine = create_engine(database)
    day_7 = pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=7)
    seed(engine, data, until=day_7.to_pydatetime())

    counts = seed(engine, data, until=(day_7 + pd.Timedelta(days=7)).to_pydatetime())

    in_week_2 = market.transactions["occurred_at"].between(
        day_7, day_7 + pd.Timedelta(days=7), inclusive="left"
    )
    assert counts["wallets"] == 0
    assert counts["transactions"] == in_week_2.sum()


def test_no_ground_truth_reaches_postgres(market: Market, data: Path, database: str):
    engine = create_engine(database)
    seed(engine, data, until=(pd.Timestamp(START, tz="UTC") + 21 * DAY).to_pydatetime())

    inspector = sa.inspect(engine)
    tables = set(inspector.get_table_names())
    # Fraud labels are the one place a Fraud discovery lands, and only from a `fraud.discovered`
    # event at its event time: seeding never writes them.
    assert _count(engine, "fraud_labels") == 0
    columns = {
        c["name"] for table in tables - {"fraud_labels"} for c in inspector.get_columns(table)
    }
    assert not tables & set(OFFLINE_ONLY)
    ground_truth_columns = set(market.dataset.ground_truth.columns) | set(
        market.dataset.fraud_discoveries.columns
    )
    # Only the Transaction ID, the key every table shares, is in common.
    assert columns & ground_truth_columns == {"transaction_id"}


def test_seeding_refuses_an_unmigrated_database(
    data: Path, empty_database: str, capsys: pytest.CaptureFixture[str]
):
    exit_code = main(["seed", "--data", str(data), "--database-url", empty_database])

    assert exit_code == 1
    assert "txrisk migrate" in capsys.readouterr().err


@pytest.fixture(scope="module")
def seeded(market: Market, data: Path, postgres: str, migrated_template: str) -> str:
    """A database holding the whole market."""
    url = create_database(postgres, "whole_market", template=migrated_template)
    engine = create_engine(url)
    seed(engine, data, until=(pd.Timestamp(START, tz="UTC") + 30 * DAY).to_pydatetime())
    engine.dispose()
    return url


def test_postgres_history_gives_exactly_the_features_of_the_offline_replay(
    market: Market, seeded: str
):
    """Postgres holds every Transaction, later ones included; the features must still see only
    what came strictly before each Transaction, as the offline replay does."""
    sample = pd.concat(
        [
            market.transactions.sample(300, random_state=3),
            # Every type with its own history query, and Transactions just after SIM swaps.
            market.transactions[market.transactions["transaction_type"] == "cash_out"].sample(
                50, random_state=4
            ),
            market.transactions[
                market.transactions["subject_wallet_id"].isin(market.sim_swaps["wallet_id"])
            ].head(100),
        ]
    ).drop_duplicates("transaction_id")
    engine = create_engine(seeded)

    with engine.connect() as connection:
        served = [
            transaction_features(
                t,
                PostgresHistory(connection),
                now=t.occurred_at,
                config=market.feature_config,
            )
            for t in transactions_from_frame(sample)
        ]

    offline = market.features.loc[sample.index].reset_index(drop=True)
    pd.testing.assert_frame_equal(
        features_frame(sample["transaction_id"].tolist(), served), offline
    )
