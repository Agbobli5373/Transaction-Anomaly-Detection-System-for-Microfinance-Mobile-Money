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
    sim_swaps: pd.DataFrame
    # Which Transactions are fraudulent. Only offline evaluation may read it (ADR 0007).
    ground_truth: pd.DataFrame
    # Complaints and chargebacks: how fraud comes to light, dated when it does. The running
    # system may see these, from their discovery time onward.
    fraud_discoveries: pd.DataFrame
    # Which Transactions are legitimate but unusual, and why. Offline evaluation only.
    benign_anomalies: pd.DataFrame


def write_dataset(dataset: Dataset, out_dir: Path) -> None:
    """Write each table as `<name>.parquet`; identical datasets produce identical bytes."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for field in fields(dataset):
        frame: pd.DataFrame = getattr(dataset, field.name)
        table = pa.Table.from_pandas(frame, preserve_index=False)
        pq.write_table(table, out_dir / f"{field.name}.parquet")
