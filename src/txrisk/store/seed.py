"""Seeding Postgres from a generated dataset: the parties, and the history before a cutoff.

Only the tables the running system may see are read. Ground truth, Fraud discoveries and Benign
anomalies are never opened (ADR 0007), so the seeded history carries no labels at all.
"""

import io
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg
import sqlalchemy as sa

from txrisk.store.tables import agents, billers, merchants, sim_swaps, transactions, wallets

# Each table loaded, in an order that satisfies the foreign keys.
SEEDED_TABLES = (wallets, agents, merchants, billers, sim_swaps, transactions)
# Tables held to the cutoff; the parties are registered whenever they first appear.
_HISTORY = {"sim_swaps", "transactions"}
_CHUNK_ROWS = 200_000


def _chunks(frame: pd.DataFrame) -> Iterator[pd.DataFrame]:
    for start in range(0, len(frame), _CHUNK_ROWS):
        yield frame.iloc[start : start + _CHUNK_ROWS]


def _prepared(frame: pd.DataFrame, table: sa.Table) -> pd.DataFrame:
    """The table's columns, with times cut to the microseconds Postgres keeps.

    Truncating matches the offline features, which read times as Python datetimes.
    """
    frame = frame[[c.name for c in table.columns]].copy()
    for column in frame.columns:
        if isinstance(frame[column].dtype, pd.DatetimeTZDtype):
            frame[column] = frame[column].dt.floor("us")
    return frame


def _load(connection: sa.Connection, table: sa.Table, frame: pd.DataFrame) -> int:
    """COPY the rows into a staging table, then insert those not already stored."""
    columns = ", ".join(c.name for c in table.columns)
    driver = connection.connection.driver_connection
    assert isinstance(driver, psycopg.Connection)
    cursor = driver.cursor()
    cursor.execute(f"CREATE TEMP TABLE staging (LIKE {table.name})")
    with cursor.copy(f"COPY staging ({columns}) FROM STDIN (FORMAT csv)") as copy:
        for chunk in _chunks(frame):
            buffer = io.StringIO()
            chunk.to_csv(buffer, index=False, header=False)
            copy.write(buffer.getvalue())
    cursor.execute(f"INSERT INTO {table.name} SELECT * FROM staging ON CONFLICT DO NOTHING")
    inserted = int(cursor.rowcount)
    cursor.execute("DROP TABLE staging")
    return inserted


def seed(engine: sa.Engine, data: Path, *, until: datetime) -> dict[str, int]:
    """Load the dataset in `data` as history: every party, and the SIM swaps and Transactions
    that occurred before `until`. Returns the rows newly stored per table; seeding again adds
    only what is missing."""
    counts = {}
    with engine.begin() as connection:
        for table in SEEDED_TABLES:
            frame = _prepared(pd.read_parquet(data / f"{table.name}.parquet"), table)
            if table.name in _HISTORY:
                frame = frame[frame["occurred_at"] < until]
            counts[table.name] = _load(connection, table, frame)
    return counts
