import pandas as pd
import pytest
from conftest import KEY

from txrisk.generator import Dataset, GeneratorConfig, generate

KINDS = {"salary_inflow", "large_payment", "travel", "new_phone"}


@pytest.fixture(scope="module")
def benign_config(small_config: GeneratorConfig) -> GeneratorConfig:
    """Forty days from 1 January: a New Year festive season and one month's pay days."""
    return small_config.model_copy(
        update={
            "period": small_config.period.model_copy(update={"days": 40}),
            "population": small_config.population.model_copy(
                update={"wallets": 800, "agents": 40, "merchants": 30}
            ),
        }
    )


@pytest.fixture(scope="module")
def benign_dataset(benign_config: GeneratorConfig) -> Dataset:
    return generate(benign_config, seed=9, key=KEY)


def _of_kind(dataset: Dataset, kind: str) -> pd.DataFrame:
    labelled = dataset.benign_anomalies[dataset.benign_anomalies["kind"] == kind]
    return labelled.merge(dataset.transactions, on="transaction_id")


def _about(count: int, share: float, population: int) -> bool:
    return 0.5 * share * population <= count <= 1.5 * share * population


def test_benign_anomalies_are_labelled_separately_and_are_never_fraud(benign_dataset: Dataset):
    benign = benign_dataset.benign_anomalies

    assert list(benign.columns) == ["transaction_id", "kind"]
    assert set(benign["kind"]) == KINDS
    assert set(benign["transaction_id"]) <= set(benign_dataset.transactions["transaction_id"])
    assert not set(benign["transaction_id"]) & set(benign_dataset.ground_truth["transaction_id"])


def test_salaried_wallets_bring_in_their_pay_around_pay_day(
    benign_dataset: Dataset, benign_config: GeneratorConfig
):
    params = benign_config.benign
    pay = _of_kind(benign_dataset, "salary_inflow")

    assert (pay["transaction_type"] == "cash_in").all()
    assert pay["occurred_at"].dt.day.between(params.pay_day.low, params.pay_day.high).all()
    assert (pay["amount"] <= params.pay_amount.high).all()
    assert _about(
        pay["subject_wallet_id"].nunique(), params.salaried_share, len(benign_dataset.wallets)
    )


def test_some_wallets_make_a_one_off_large_payment(
    benign_dataset: Dataset, benign_config: GeneratorConfig
):
    params = benign_config.benign
    usual = benign_config.behaviour.transaction_types["payment"].median_amount
    large = _of_kind(benign_dataset, "large_payment")

    assert (large["transaction_type"] == "payment").all()
    assert large["subject_wallet_id"].is_unique
    assert (large["amount"] >= params.large_payment_multiple.low * usual * 0.99).all()
    assert _about(len(large), params.large_payment_share, len(benign_dataset.wallets))


def _legitimate(dataset: Dataset) -> pd.DataFrame:
    tx = dataset.transactions
    return tx[~tx["transaction_id"].isin(dataset.ground_truth["transaction_id"])]


def test_travellers_transact_away_from_home_for_a_few_days(
    benign_dataset: Dataset, benign_config: GeneratorConfig
):
    params = benign_config.benign
    trips = _of_kind(benign_dataset, "travel")
    home = trips["subject_wallet_id"].map(
        benign_dataset.wallets.set_index("wallet_id")["home_region"]
    )
    span = trips.groupby("subject_wallet_id")["occurred_at"].agg(lambda t: t.max() - t.min())

    assert (trips["region"] != home).all()
    assert trips.groupby("subject_wallet_id")["region"].nunique().eq(1).all()
    assert (span <= pd.Timedelta(days=params.travel_days.high)).all()
    assert _about(
        trips["subject_wallet_id"].nunique(), params.travel_share, len(benign_dataset.wallets)
    )


def test_a_new_phone_replaces_the_old_one_for_good(
    benign_dataset: Dataset, benign_config: GeneratorConfig
):
    params = benign_config.benign
    legitimate = _legitimate(benign_dataset)
    firsts = _of_kind(benign_dataset, "new_phone")
    moved_to_app = 0

    assert firsts["subject_wallet_id"].is_unique
    assert _about(len(firsts), params.new_phone_share, len(benign_dataset.wallets))
    for _, first in firsts.iterrows():
        own = legitimate[legitimate["subject_wallet_id"] == first["subject_wallet_id"]]
        before = own[own["occurred_at"] < first["occurred_at"]]
        after = own[own["occurred_at"] >= first["occurred_at"]]
        assert before["device_id"].nunique() <= 1
        assert first["device_id"] not in set(before["device_id"])
        assert (after["device_id"] == first["device_id"]).all()
        if (
            len(before)
            and (before["channel"] == "ussd").all()
            and (after["channel"] == "app").all()
        ):
            moved_to_app += 1
    assert moved_to_app > 0


def test_festive_seasons_bring_more_and_larger_transactions(benign_dataset: Dataset):
    legitimate = _legitimate(benign_dataset)
    day = legitimate["occurred_at"].dt.normalize()
    festive = day.isin(pd.to_datetime(["2026-01-01", "2026-01-02"], utc=True))
    weekday = legitimate["occurred_at"].dt.day_of_week < 5
    per_day = legitimate.groupby(day).size()
    # Compare with the ordinary weekdays straight after, since the market keeps growing.
    ordinary_days = per_day[
        ~per_day.index.isin(day[festive]) & (per_day.index.day_of_week < 5)
    ].iloc[:10]

    assert per_day[day[festive].unique()].mean() >= 1.3 * ordinary_days.mean()
    assert (
        legitimate.loc[festive, "amount"].median()
        >= 1.15 * legitimate.loc[~festive & weekday, "amount"].median()
    )
