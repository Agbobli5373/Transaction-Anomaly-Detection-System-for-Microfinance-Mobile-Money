"""The generated dataset and how it is written to disk."""

from dataclasses import dataclass, fields
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class Dataset:
    wallets: pd.DataFrame
    agents: pd.DataFrame
    merchants: pd.DataFrame
    billers: pd.DataFrame
    transactions: pd.DataFrame


def write_dataset(dataset: Dataset, out_dir: Path) -> None:
    """Write each table as `<name>.parquet`; identical datasets produce identical bytes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for field in fields(dataset):
        frame: pd.DataFrame = getattr(dataset, field.name)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_table(table, out_dir / f"{field.name}.parquet")
