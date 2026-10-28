"""Seeding Postgres from a generated dataset: the parties, and the history before a cutoff.

Only what the running system may see is read: the parties, SIM swaps, Transactions and Fraud
discoveries. Ground truth and Benign anomalies are never opened (ADR 0007).

A Fraud discovery is seeded as a Fraud label, dated by its own event time, only if it happened
before the cutoff. That is what a running system would hold at the cutoff had it been receiving
`fraud.discovered` events all along, and it is what retraining learns from once the live period
starts: without these labels the first retrain would see no Fraud at all. A discovery after the
cutoff is not seeded; the replay publishes it at its event time, as it would arrive.
"""

import io
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg
import sqlalchemy as sa

from txrisk.store.tables import (
    agents,
    billers,
    fraud_labels,
    merchants,
    sim_swaps,
    transactions,
    wallets,
)

# Each table loaded, in an order that satisfies the foreign keys.
SEEDED_TABLES = (wallets, agents, merchants, billers, sim_swaps, transactions, fraud_labels)
# Tables held to the cutoff, by the event-time column that decides it; the parties are
# registered whenever they first appear.
_HISTORY = {
    "sim_swaps": "occurred_at",
    "transactions": "occurred_at",
    "fraud_labels": "discovered_at",
}
# The dataset file a table is loaded from, where it is not named for the table: a Fraud label is
# what a Fraud discovery becomes.
_SOURCE_FILE = {"fraud_labels": "fraud_discoveries"}
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
    that occurred, and the Fraud discoveries that were made, before `until`. Returns the rows
    newly stored per table; seeding again adds only what is missing."""
    counts = {}
    with engine.begin() as connection:
        for table in SEEDED_TABLES:
            source = _SOURCE_FILE.get(table.name, table.name)
            frame = _prepared(pd.read_parquet(data / f"{source}.parquet"), table)
            if table.name in _HISTORY:
                frame = frame[frame[_HISTORY[table.name]] < until]
            counts[table.name] = _load(connection, table, frame)
    return counts
