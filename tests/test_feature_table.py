from pathlib import Path

import pandas as pd
import pytest
from conftest import KEY

from txrisk.cli import main
from txrisk.generator import GeneratorConfig, generate, write_dataset

HISTORY_DEPENDENT = [
    "velocity_count_1h",
    "velocity_amount_1h",
    "velocity_count_24h",
    "velocity_amount_24h",
    "velocity_count_7d",
    "velocity_amount_7d",
    "amount_to_median_30d",
    "seconds_since_last_transaction",
    "new_counterparty",
    "unusual_hour",
    "new_region_30d",
]


@pytest.fixture(scope="module")
def data_dir(small_config: GeneratorConfig, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("generated")
    write_dataset(generate(small_config, seed=5, key=KEY), out)
    return out


def _features(data: Path, out: Path) -> pd.DataFrame:
    assert main(["features", "--data", str(data), "--out", str(out)]) == 0
    return pd.read_parquet(out)


def test_writes_one_row_of_features_per_transaction(data_dir: Path, tmp_path: Path):
    transactions = pd.read_parquet(data_dir / "transactions.parquet")

    features = _features(data_dir, tmp_path / "features.parquet")

    assert list(features["transaction_id"]) == list(transactions["transaction_id"])
    assert features["history_count"].notna().all()
    assert features["wallet_age_days"].notna().all()


def test_each_transaction_counts_the_wallets_earlier_transactions(data_dir: Path, tmp_path: Path):
    transactions = pd.read_parquet(data_dir / "transactions.parquet")
    # The nth Transaction of a Wallet has n - 1 before it; Transactions at the same moment
    # share the count of those strictly earlier.
    expected = (
        transactions.groupby("subject_wallet_id")["occurred_at"].rank(method="min").astype(int) - 1
    )

    features = _features(data_dir, tmp_path / "features.parquet")

    assert list(features["history_count"]) == list(expected)


def test_history_dependent_features_are_empty_only_below_the_minimum_history(
    data_dir: Path, tmp_path: Path
):
    features = _features(data_dir, tmp_path / "features.parquet")
    cold = features["history_count"] < 5

    assert features.loc[cold, HISTORY_DEPENDENT].isna().all().all()
    always_present = [c for c in HISTORY_DEPENDENT if c != "amount_to_median_30d"]
    assert features.loc[~cold, always_present].notna().all().all()


def test_features_do_not_depend_on_transactions_after_the_one_scored(
    data_dir: Path, tmp_path: Path
):
    transactions = pd.read_parquet(data_dir / "transactions.parquet")
    cutoff = transactions["occurred_at"].iloc[len(transactions) // 2]
    truncated = tmp_path / "truncated"
    truncated.mkdir()
    for table in data_dir.glob("*.parquet"):
        (truncated / table.name).write_bytes(table.read_bytes())
    transactions[transactions["occurred_at"] <= cutoff].to_parquet(
        truncated / "transactions.parquet", index=False
    )

    full = _features(data_dir, tmp_path / "full.parquet")
    partial = _features(truncated, tmp_path / "partial.parquet")

    assert len(partial) < len(full)
    pd.testing.assert_frame_equal(partial, full.iloc[: len(partial)])
