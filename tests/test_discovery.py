import numpy as np
import pandas as pd
import pytest
from conftest import KEY

from txrisk.generator import Dataset, FraudScenario, GeneratorConfig, generate
from txrisk.generator.discovery import discover


@pytest.fixture(scope="module")
def config(small_config: GeneratorConfig) -> GeneratorConfig:
    """Plenty of fraud so discovery rates can be measured per scenario."""
    return small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 1500, "agents": 60, "merchants": 30}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )


@pytest.fixture(scope="module")
def dataset(config: GeneratorConfig) -> Dataset:
    return generate(config, seed=13, key=KEY)


@pytest.fixture(scope="module")
def discovered(dataset: Dataset) -> pd.DataFrame:
    return dataset.fraud_discoveries.merge(dataset.ground_truth, on="transaction_id").merge(
        dataset.transactions, on="transaction_id"
    )


def test_discoveries_are_dated_events_about_fraudulent_transactions(dataset: Dataset):
    found = dataset.fraud_discoveries

    assert list(found.columns) == ["transaction_id", "discovered_at", "source"]
    assert found["transaction_id"].is_unique
    assert set(found["transaction_id"]) <= set(dataset.ground_truth["transaction_id"])
    assert set(found["source"]) == {"complaint", "chargeback"}


def test_fraud_is_discovered_days_to_weeks_after_it_happens(
    discovered: pd.DataFrame, config: GeneratorConfig
):
    delay = config.fraud.discovery.delay_days
    days_later = (discovered["discovered_at"] - discovered["occurred_at"]) / pd.Timedelta(days=1)

    assert (days_later >= delay.low).all()
    # Measured from the episode's last Transaction, so earlier ones can wait a little longer.
    assert (days_later <= delay.high + 2).all()


def test_one_report_covers_a_victims_whole_episode(discovered: pd.DataFrame):
    single_victim = discovered[
        discovered["scenario"].isin(
            [FraudScenario.SIM_SWAP_TAKEOVER.value, FraudScenario.ACCOUNT_TAKEOVER_DRAIN.value]
        )
    ]

    assert (single_victim.groupby("instance_id")["discovered_at"].nunique() == 1).all()


def test_each_scenario_is_reported_about_as_often_as_configured(
    dataset: Dataset, discovered: pd.DataFrame, config: GeneratorConfig
):
    probability = config.fraud.discovery.probability
    truth = dataset.ground_truth
    sender_or_victim = truth.merge(dataset.transactions, on="transaction_id")

    for scenario in [
        FraudScenario.SIM_SWAP_TAKEOVER,
        FraudScenario.ACCOUNT_TAKEOVER_DRAIN,
        FraudScenario.SOCIAL_ENGINEERING_PUSH,
    ]:
        episodes = sender_or_victim.loc[
            truth["scenario"] == scenario.value, "instance_id"
        ].nunique()
        reported = discovered.loc[discovered["scenario"] == scenario.value, "instance_id"].nunique()
        assert abs(reported / episodes - probability[scenario]) < 0.15, scenario


def test_some_fraud_is_never_reported(dataset: Dataset):
    assert len(dataset.fraud_discoveries) < len(dataset.ground_truth)


def test_chargebacks_are_the_configured_minority(dataset: Dataset, config: GeneratorConfig):
    share = (dataset.fraud_discoveries["source"] == "chargeback").mean()

    assert abs(share - config.fraud.discovery.chargeback_share) < 0.15


def test_a_mules_cash_outs_come_to_light_with_its_senders_complaints(discovered: pd.DataFrame):
    mule = discovered[discovered["scenario"] == FraudScenario.MULE_FAN_IN.value]
    cash_outs = mule[mule["transaction_type"] == "cash_out"]
    complaints = mule[mule["transaction_type"] == "transfer"]

    assert len(cash_outs) > 0
    assert set(cash_outs["instance_id"]) <= set(complaints["instance_id"])


def test_a_victims_report_does_not_depend_on_other_episodes(
    dataset: Dataset, config: GeneratorConfig
):
    frauds = dataset.ground_truth.merge(
        dataset.transactions[
            ["transaction_id", "subject_wallet_id", "transaction_type", "occurred_at"]
        ],
        on="transaction_id",
    )
    first_instance = frauds["instance_id"].min()

    everything = discover(config, frauds, np.random.default_rng(1))
    without_one = discover(
        config, frauds[frauds["instance_id"] != first_instance], np.random.default_rng(1)
    )

    others = everything[everything["transaction_id"].isin(without_one["transaction_id"])]
    pd.testing.assert_frame_equal(
        others.reset_index(drop=True),
        without_one.reset_index(drop=True),
    )
    assert len(without_one) > 0
