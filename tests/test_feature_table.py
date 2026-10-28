from datetime import UTC, datetime
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


def test_every_transaction_type_gets_the_features_that_apply_to_it(data_dir: Path, tmp_path: Path):
    transactions = pd.read_parquet(data_dir / "transactions.parquet")
    kind = transactions["transaction_type"]

    features = _features(data_dir, tmp_path / "features.parquet")

    assert set(kind) == {"cash_in", "cash_out", "transfer", "payment", "bill_payment"}
    fan_in = kind.isin(["transfer", "cash_out"])
    assert features.loc[fan_in, "receiver_fan_in_24h"].notna().all()
    assert features.loc[~fan_in, "receiver_fan_in_24h"].isna().all()
    transfer = kind == "transfer"
    assert features.loc[transfer, "received_from_counterparty_24h"].notna().all()
    assert features.loc[~transfer, "received_from_counterparty_24h"].isna().all()
    agent = kind.isin(["cash_in", "cash_out"])
    agent_columns = [
        "agent_cash_out_count_24h",
        "agent_cash_out_amount_24h",
        "agent_near_limit_cash_out_count_24h",
    ]
    assert features.loc[agent, agent_columns].notna().all().all()
    assert features.loc[~agent, agent_columns].isna().all().all()
    outgoing = transactions["direction"] == "out"
    assert features.loc[outgoing, "share_of_balance_moved"].between(0, 1, "right").all()
    assert features.loc[~outgoing, "share_of_balance_moved"].isna().all()
    assert features["seconds_since_sim_swap"].notna().any()


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 2, 1, hour, minute, tzinfo=UTC)


def _cash(kind: str, wallet: str, at: datetime, amount: float) -> dict[str, object]:
    return {
        "transaction_id": f"T-{wallet}-{at:%H%M}",
        "occurred_at": at,
        "transaction_type": kind,
        "amount": amount,
        "subject_wallet_id": wallet,
        "direction": "in" if kind == "cash_in" else "out",
        "subject_balance_before": 10_000.0,
        "channel": "ussd",
        "device_id": f"D-{wallet}",
        "region": "Ashanti",
        "counterparty_wallet_id": None,
        "agent_id": "A00001",
        "merchant_id": None,
        "biller_id": None,
    }


@pytest.fixture
def agent_day(tmp_path: Path) -> Path:
    """Three Cash-outs at one Agent by a minimum-tier and a medium-tier Wallet, then a Cash-in."""
    data = tmp_path / "agent-day"
    data.mkdir()
    pd.DataFrame(
        [
            _cash("cash_out", "w-min", _at(9), 950.0),
            _cash("cash_out", "w-med", _at(9, 30), 950.0),
            _cash("cash_out", "w-med", _at(10), 4600.0),
            _cash("cash_in", "w-min", _at(11), 20.0),
        ]
    ).to_parquet(data / "transactions.parquet", index=False)
    pd.DataFrame(
        {
            "wallet_id": ["w-min", "w-med"],
            "kyc_tier": ["minimum", "medium"],
            "home_region": ["Ashanti", "Ashanti"],
            "registered_at": [datetime(2026, 1, 1, tzinfo=UTC)] * 2,
        }
    ).to_parquet(data / "wallets.parquet", index=False)
    pd.DataFrame({"wallet_id": ["w-min"], "occurred_at": [_at(10, 30)]}).to_parquet(
        data / "sim_swaps.parquet", index=False
    )
    return data


def test_near_limit_cash_outs_use_the_kyc_tier_limits_of_the_market_config(
    agent_day: Path, tmp_path: Path
):
    features = _features(agent_day, tmp_path / "features.parquet")

    cash_in = features.iloc[3]
    assert cash_in["agent_cash_out_count_24h"] == 3
    assert cash_in["agent_cash_out_amount_24h"] == 6500.0
    # The market config limits the minimum tier to 1000 and the medium tier to 5000, so 950
    # is near-limit for w-min but not for w-med, and 4600 is near-limit for w-med.
    assert cash_in["agent_near_limit_cash_out_count_24h"] == 2


def test_the_near_limit_share_comes_from_the_feature_config(agent_day: Path, tmp_path: Path):
    feature_config = tmp_path / "features.toml"
    feature_config.write_text("min_history = 5\nnear_limit_share = 0.05\n")
    out = tmp_path / "features.parquet"

    assert (
        main(
            [
                "features",
                "--data",
                str(agent_day),
                "--feature-config",
                str(feature_config),
                "--out",
                str(out),
            ]
        )
        == 0
    )

    # Within 5% below: 950 against 1000 still is, 4600 against 5000 no longer is.
    assert pd.read_parquet(out).iloc[3]["agent_near_limit_cash_out_count_24h"] == 1


def test_time_since_sim_swap_sees_only_sim_swaps_before_each_transaction(
    agent_day: Path, tmp_path: Path
):
    features = _features(agent_day, tmp_path / "features.parquet")

    # w-min's SIM swap at 10:30 is after its 09:00 Cash-out and 30 minutes before its Cash-in.
    assert pd.isna(features.iloc[0]["seconds_since_sim_swap"])
    assert features.iloc[3]["seconds_since_sim_swap"] == 1800.0
    assert pd.isna(features.iloc[1]["seconds_since_sim_swap"])
