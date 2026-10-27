import itertools
from pathlib import Path

import pandas as pd
import pytest
from conftest import KEY

from txrisk.generator import Dataset, GeneratorConfig, generate, write_dataset

OUTPUT_FILES = [
    "wallets",
    "agents",
    "merchants",
    "billers",
    "transactions",
    "sim_swaps",
    "ground_truth",
    "fraud_discoveries",
    "benign_anomalies",
]
COUNTERPARTY_COLUMNS = {
    "counterparty_wallet_id": "wallets",
    "agent_id": "agents",
    "merchant_id": "merchants",
    "biller_id": "billers",
}
EXPECTED_COUNTERPARTY = {
    "cash_in": "agent_id",
    "cash_out": "agent_id",
    "transfer": "counterparty_wallet_id",
    "payment": "merchant_id",
    "bill_payment": "biller_id",
}


@pytest.fixture(scope="module")
def dataset(small_config: GeneratorConfig) -> Dataset:
    return generate(small_config, seed=11, key=KEY)


def _write(config: GeneratorConfig, seed: int, out: Path) -> dict[str, bytes]:
    write_dataset(generate(config, seed=seed, key=KEY), out)
    return {name: (out / f"{name}.parquet").read_bytes() for name in OUTPUT_FILES}


def test_same_seed_produces_byte_identical_output(small_config: GeneratorConfig, tmp_path: Path):
    first = _write(small_config, seed=7, out=tmp_path / "a")
    second = _write(small_config, seed=7, out=tmp_path / "b")

    assert first == second


def test_different_seed_produces_different_transactions(
    small_config: GeneratorConfig, tmp_path: Path
):
    first = _write(small_config, seed=7, out=tmp_path / "a")
    second = _write(small_config, seed=8, out=tmp_path / "b")

    assert first["transactions"] != second["transactions"]


def test_every_transaction_type_occurs(dataset: Dataset):
    assert set(dataset.transactions["transaction_type"]) == set(EXPECTED_COUNTERPARTY)


def test_each_transaction_has_exactly_one_counterparty_of_the_right_kind(dataset: Dataset):
    tx = dataset.transactions
    present = tx[list(COUNTERPARTY_COLUMNS)].notna()

    assert (present.sum(axis=1) == 1).all()
    for transaction_type, column in EXPECTED_COUNTERPARTY.items():
        of_type = present[tx["transaction_type"] == transaction_type]
        assert of_type[column].all(), transaction_type


def test_counterparties_exist_in_the_population(dataset: Dataset):
    tx = dataset.transactions
    for column, table in COUNTERPARTY_COLUMNS.items():
        id_column = "wallet_id" if table == "wallets" else column
        known = set(getattr(dataset, table)[id_column])
        assert set(tx[column].dropna()) <= known, column


def test_a_wallet_never_transfers_to_itself(dataset: Dataset):
    tx = dataset.transactions
    transfers = tx[tx["transaction_type"] == "transfer"]

    assert (transfers["subject_wallet_id"] != transfers["counterparty_wallet_id"]).all()


def test_cash_in_moves_money_into_the_subject_wallet_and_everything_else_out(dataset: Dataset):
    tx = dataset.transactions
    expected = tx["transaction_type"].map(lambda t: "in" if t == "cash_in" else "out")

    assert (tx["direction"] == expected).all()


def _with_tier(dataset: Dataset, config: GeneratorConfig) -> pd.DataFrame:
    tiers = pd.DataFrame([t.model_dump() for t in config.market.kyc_tiers])
    wallets = dataset.wallets.merge(tiers, left_on="kyc_tier", right_on="name")
    return dataset.transactions.merge(
        wallets[["wallet_id", "single_transaction_limit", "max_balance"]],
        left_on="subject_wallet_id",
        right_on="wallet_id",
    )


def test_no_transaction_exceeds_the_kyc_tier_single_transaction_limit(
    dataset: Dataset, small_config: GeneratorConfig
):
    tx = _with_tier(dataset, small_config)

    assert (tx["amount"] > 0).all()
    assert (tx["amount"] <= tx["single_transaction_limit"]).all()


def test_balances_stay_within_zero_and_the_kyc_tier_max_balance(
    dataset: Dataset, small_config: GeneratorConfig
):
    tx = _with_tier(dataset, small_config)
    outgoing = tx[tx["direction"] == "out"]

    assert (tx["subject_balance_before"] >= 0).all()
    assert (tx["subject_balance_before"] <= tx["max_balance"]).all()
    assert (outgoing["amount"] <= outgoing["subject_balance_before"]).all()


def test_each_wallets_balance_reconciles_with_its_transactions(dataset: Dataset):
    tx = dataset.transactions
    as_subject = pd.DataFrame(
        {
            "wallet_id": tx["subject_wallet_id"],
            "position": tx.index,
            "change": tx["amount"].where(tx["direction"] == "in", -tx["amount"]),
            "balance_before": tx["subject_balance_before"],
        }
    )
    transfers = tx[tx["transaction_type"] == "transfer"]
    as_receiver = pd.DataFrame(
        {
            "wallet_id": transfers["counterparty_wallet_id"].astype(object),
            "position": transfers.index,
            "change": transfers["amount"],
            "balance_before": float("nan"),
        }
    )
    ledger = pd.concat([as_subject, as_receiver]).sort_values(["wallet_id", "position"])

    # Between two of a Wallet's own Transactions, its balance moves by exactly the net of
    # everything in between, including Transfers it received.
    for _, entries in ledger.groupby("wallet_id"):
        cents = (entries["change"] * 100).round().astype(int).to_numpy()
        before = (entries["balance_before"] * 100).round()
        own = before.notna().to_numpy().nonzero()[0]
        for earlier, later in itertools.pairwise(own):
            expected = before.iloc[earlier] + cents[earlier:later].sum()
            assert before.iloc[later] == expected


def test_wallet_ids_are_pseudonyms_and_no_phone_numbers_or_names_are_output(dataset: Dataset):
    msisdn = r"^(?:\+?233|0)\d{9}$"

    assert dataset.wallets["wallet_id"].str.fullmatch(r"[0-9a-f]{64}").all()
    assert dataset.wallets["wallet_id"].is_unique
    for name in OUTPUT_FILES:
        table: pd.DataFrame = getattr(dataset, name)
        assert not {"msisdn", "phone", "name"} & set(table.columns), name
        for column in table.select_dtypes(include=["object", "string"]).columns:
            assert not table[column].dropna().astype(str).str.fullmatch(msisdn).any(), column


def test_pseudonyms_depend_on_the_key(small_config: GeneratorConfig, dataset: Dataset):
    same_key = generate(small_config, seed=11, key=KEY)
    other_key = generate(small_config, seed=11, key=b"another-key")

    assert same_key.wallets["wallet_id"].equals(dataset.wallets["wallet_id"])
    assert not set(other_key.wallets["wallet_id"]) & set(dataset.wallets["wallet_id"])


def _period(config: GeneratorConfig) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = pd.Timestamp(config.period.start, tz="UTC")
    return start, start + pd.Timedelta(days=config.period.days)


def test_wallets_register_before_and_throughout_the_period(
    dataset: Dataset, small_config: GeneratorConfig
):
    start, end = _period(small_config)
    registered = dataset.wallets["registered_at"]
    during = registered[registered >= start]

    assert (registered < start).any()
    assert (registered < end).all()
    # New registrations reach every third of the period, not just its first days.
    thirds = ((during - start) / (end - start) * 3).astype(int)
    assert set(thirds) == {0, 1, 2}


def test_no_wallet_takes_part_in_a_transaction_before_it_registers(dataset: Dataset):
    registered = dataset.wallets.set_index("wallet_id")["registered_at"]
    tx = dataset.transactions
    transfers = tx[tx["transaction_type"] == "transfer"]

    assert (tx["occurred_at"] >= tx["subject_wallet_id"].map(registered)).all()
    receiver_registered = transfers["counterparty_wallet_id"].astype(object).map(registered)
    assert (transfers["occurred_at"] >= receiver_registered).all()


def test_wallets_registered_during_the_period_start_with_a_zero_balance(
    dataset: Dataset, small_config: GeneratorConfig
):
    start, _ = _period(small_config)
    new = set(dataset.wallets.loc[dataset.wallets["registered_at"] >= start, "wallet_id"])
    tx = dataset.transactions
    transfers = tx[tx["transaction_type"] == "transfer"]
    touches = pd.concat(
        [
            pd.DataFrame({"wallet_id": tx["subject_wallet_id"], "as_subject": True}),
            pd.DataFrame(
                {
                    "wallet_id": transfers["counterparty_wallet_id"].astype(object),
                    "as_subject": False,
                }
            ),
        ]
    ).sort_index(kind="stable")
    # The first Transaction touching each new Wallet, where the Wallet is the Subject wallet.
    first = touches[touches["wallet_id"].isin(new)].drop_duplicates("wallet_id")
    first_own = first[first["as_subject"]]

    assert len(first_own) > 0
    assert (tx.loc[first_own.index, "subject_balance_before"] == 0).all()


def test_transactions_fall_inside_the_period_in_utc(
    dataset: Dataset, small_config: GeneratorConfig
):
    start, end = _period(small_config)
    occurred = dataset.transactions["occurred_at"]

    assert str(occurred.dt.tz) == "UTC"
    assert occurred.between(start, end, inclusive="left").all()
    assert occurred.is_monotonic_increasing


def test_activity_follows_a_daily_rhythm(dataset: Dataset):
    hours = dataset.transactions["occurred_at"].dt.hour
    night = hours.between(0, 4).sum() / 5
    day = hours.between(8, 19).sum() / 12

    assert night < 0.25 * day


def test_channel_is_ussd_or_app_with_ussd_the_majority(dataset: Dataset):
    channels = dataset.transactions["channel"]

    assert set(channels) == {"ussd", "app"}
    assert 0.5 < (channels == "ussd").mean() < 0.9


def test_in_legitimate_activity_each_wallet_uses_its_own_device_until_a_new_phone(
    dataset: Dataset,
):
    tx = dataset.transactions
    legitimate = tx[~tx["transaction_id"].isin(dataset.ground_truth["transaction_id"])]
    devices = legitimate.groupby("subject_wallet_id")["device_id"].nunique()
    owners = legitimate.groupby("device_id")["subject_wallet_id"].nunique()
    new_phone = dataset.benign_anomalies.loc[
        dataset.benign_anomalies["kind"] == "new_phone", "transaction_id"
    ]
    switched = set(tx.loc[tx["transaction_id"].isin(new_phone), "subject_wallet_id"])

    assert (owners == 1).all()
    assert (devices <= 2).all()
    assert set(devices[devices == 2].index) <= switched


def test_transactions_mostly_happen_in_the_home_region_at_local_counterparties(
    dataset: Dataset, small_config: GeneratorConfig
):
    tx = dataset.transactions
    home = tx["subject_wallet_id"].map(dataset.wallets.set_index("wallet_id")["home_region"])
    agent_region = tx["agent_id"].astype(object).map(dataset.agents.set_index("agent_id")["region"])
    merchant_region = (
        tx["merchant_id"].astype(object).map(dataset.merchants.set_index("merchant_id")["region"])
    )

    # Transfers and Bill payments happen wherever the Wallet holder is, which is mostly home.
    remote = tx["transaction_type"].isin(["transfer", "bill_payment"])

    assert set(tx["region"]) <= set(small_config.market.regions)
    assert (tx.loc[remote, "region"] == home[remote]).mean() > 0.85
    assert (tx.loc[agent_region.notna(), "region"] == agent_region.dropna()).all()
    assert (tx.loc[merchant_region.notna(), "region"] == merchant_region.dropna()).all()
