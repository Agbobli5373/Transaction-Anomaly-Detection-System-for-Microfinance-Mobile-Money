import pandas as pd
import pytest
from conftest import KEY

from txrisk.generator import Dataset, FraudScenario, GeneratorConfig, generate

TRANSACTION_COLUMNS = [
    "transaction_id",
    "occurred_at",
    "transaction_type",
    "amount",
    "subject_wallet_id",
    "direction",
    "subject_balance_before",
    "channel",
    "device_id",
    "region",
    "counterparty_wallet_id",
    "agent_id",
    "merchant_id",
    "biller_id",
]


@pytest.fixture(scope="module")
def fraud_config(small_config: GeneratorConfig) -> GeneratorConfig:
    """The small market with far more fraud, so every scenario has many instances."""
    return small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 800, "agents": 40, "merchants": 20}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )


@pytest.fixture(scope="module")
def fraud_dataset(fraud_config: GeneratorConfig) -> Dataset:
    return generate(fraud_config, seed=5, key=KEY)


def _fraud(dataset: Dataset, scenario: FraudScenario | None = None) -> pd.DataFrame:
    truth = dataset.ground_truth
    if scenario is not None:
        truth = truth[truth["scenario"] == scenario.value]
    return truth.merge(dataset.transactions, on="transaction_id").sort_values("occurred_at")


def test_transactions_carry_no_fraud_indicator(fraud_dataset: Dataset):
    assert list(fraud_dataset.transactions.columns) == TRANSACTION_COLUMNS


def test_ground_truth_names_settled_transactions_and_their_scenario(fraud_dataset: Dataset):
    truth = fraud_dataset.ground_truth

    assert list(truth.columns) == ["transaction_id", "scenario", "instance_id"]
    assert truth["transaction_id"].is_unique
    assert set(truth["transaction_id"]) <= set(fraud_dataset.transactions["transaction_id"])
    assert set(truth["scenario"]) <= {s.value for s in FraudScenario}


def test_sim_swaps_are_dated_events_including_legitimate_ones(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    swaps = fraud_dataset.sim_swaps
    start = pd.Timestamp(fraud_config.period.start, tz="UTC")
    end = start + pd.Timedelta(days=fraud_config.period.days)
    defrauded = set(_fraud(fraud_dataset)["subject_wallet_id"])

    assert list(swaps.columns) == ["wallet_id", "occurred_at"]
    assert swaps["occurred_at"].between(start, end, inclusive="left").all()
    assert set(swaps["wallet_id"]) <= set(fraud_dataset.wallets["wallet_id"])
    legitimate = swaps[~swaps["wallet_id"].isin(defrauded)]
    expected = fraud_config.fraud.legitimate_sim_swap_share * fraud_config.population.wallets
    assert 0.5 * expected <= len(legitimate) <= 1.5 * expected


def _instances(dataset: Dataset, scenario: FraudScenario) -> list[pd.DataFrame]:
    fraud = _fraud(dataset, scenario)
    instances = [group for _, group in fraud.groupby("instance_id")]
    assert len(instances) >= 3, f"too few {scenario.value} instances to test"
    return instances


def _earlier(dataset: Dataset, wallet: str, before: pd.Timestamp) -> pd.DataFrame:
    tx = dataset.transactions
    return tx[(tx["subject_wallet_id"] == wallet) & (tx["occurred_at"] < before)]


def _limits(dataset: Dataset, config: GeneratorConfig) -> pd.Series:
    limits = {t.name: t.single_transaction_limit for t in config.market.kyc_tiers}
    return dataset.wallets.set_index("wallet_id")["kyc_tier"].map(limits)


def test_sim_swap_takeover_drains_a_wallet_soon_after_its_sim_swap_from_a_new_device(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    params = fraud_config.fraud.scenarios.sim_swap_takeover
    swaps = fraud_dataset.sim_swaps
    limits = _limits(fraud_dataset, fraud_config)

    for instance in _instances(fraud_dataset, FraudScenario.SIM_SWAP_TAKEOVER):
        first = instance.iloc[0]
        wallet = first["subject_wallet_id"]
        swapped = swaps.loc[
            (swaps["wallet_id"] == wallet) & (swaps["occurred_at"] <= first["occurred_at"]),
            "occurred_at",
        ].max()
        delay_hours = (first["occurred_at"] - swapped) / pd.Timedelta(hours=1)
        earlier = _earlier(fraud_dataset, wallet, first["occurred_at"])

        assert (instance["subject_wallet_id"] == wallet).all()
        assert set(instance["transaction_type"]) <= {"cash_out", "transfer"}
        assert params.delay_hours.low <= delay_hours <= params.delay_hours.high
        assert not set(instance["device_id"]) & set(earlier["device_id"])
        drained = min(params.drain_share.low * first["subject_balance_before"], limits[wallet])
        assert first["amount"] >= drained - 0.01


def test_account_takeover_drain_is_a_burst_of_transfers_to_new_counterparties_from_a_new_device(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    params = fraud_config.fraud.scenarios.account_takeover_drain

    for instance in _instances(fraud_dataset, FraudScenario.ACCOUNT_TAKEOVER_DRAIN):
        wallet = instance["subject_wallet_id"].iloc[0]
        earlier = _earlier(fraud_dataset, wallet, instance["occurred_at"].min())
        burst = instance["occurred_at"].max() - instance["occurred_at"].min()
        known = set(earlier["counterparty_wallet_id"].dropna())

        assert (instance["subject_wallet_id"] == wallet).all()
        assert (instance["transaction_type"] == "transfer").all()
        assert len(instance) >= 2
        assert burst <= pd.Timedelta(minutes=params.burst_minutes.high)
        assert instance["device_id"].nunique() == 1
        assert not set(instance["device_id"]) & set(earlier["device_id"])
        assert instance["counterparty_wallet_id"].is_unique
        assert not set(instance["counterparty_wallet_id"]) & known


def test_mule_fan_in_collects_from_many_senders_then_cashes_out(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    params = fraud_config.fraud.scenarios.mule_fan_in

    for instance in _instances(fraud_dataset, FraudScenario.MULE_FAN_IN):
        incoming = instance[instance["transaction_type"] == "transfer"]
        cash_outs = instance[instance["transaction_type"] == "cash_out"]
        mule = incoming["counterparty_wallet_id"].iloc[0]
        window = incoming["occurred_at"].max() - incoming["occurred_at"].min()
        senders = incoming["subject_wallet_id"]

        assert (incoming["counterparty_wallet_id"] == mule).all()
        assert senders.is_unique and mule not in set(senders)
        assert senders.nunique() >= params.senders.low / 2
        assert window <= pd.Timedelta(hours=params.window_hours.high)
        assert len(cash_outs) >= 1
        assert (cash_outs["subject_wallet_id"] == mule).all()
        assert cash_outs["occurred_at"].min() > incoming["occurred_at"].max()
        assert set(instance["transaction_type"]) == {"transfer", "cash_out"}


def test_agent_structuring_is_many_cash_outs_just_under_the_limit_at_one_agent(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    params = fraud_config.fraud.scenarios.agent_structuring
    limits = _limits(fraud_dataset, fraud_config)

    for instance in _instances(fraud_dataset, FraudScenario.AGENT_STRUCTURING):
        cash_outs = instance[instance["transaction_type"] == "cash_out"]
        loads = instance[instance["transaction_type"] == "cash_in"]
        agent = cash_outs["agent_id"].iloc[0]
        limit = cash_outs["subject_wallet_id"].map(limits)
        window = cash_outs["occurred_at"].max() - cash_outs["occurred_at"].min()

        assert set(instance["transaction_type"]) <= {"cash_in", "cash_out"}
        assert len(cash_outs) >= params.cash_outs.low / 2
        assert (cash_outs["agent_id"] == agent).all()
        assert cash_outs["subject_wallet_id"].is_unique
        assert (cash_outs["amount"] >= params.limit_share.low * limit - 0.01).all()
        assert (cash_outs["amount"] <= limit).all()
        assert window <= pd.Timedelta(hours=params.window_hours.high)
        assert (loads["agent_id"] != agent).all()


def test_social_engineering_push_is_one_large_transfer_from_the_victims_own_device(
    fraud_dataset: Dataset, fraud_config: GeneratorConfig
):
    params = fraud_config.fraud.scenarios.social_engineering_push
    limits = _limits(fraud_dataset, fraud_config)

    for instance in _instances(fraud_dataset, FraudScenario.SOCIAL_ENGINEERING_PUSH):
        push = instance.iloc[0]
        wallet = push["subject_wallet_id"]
        earlier = _earlier(fraud_dataset, wallet, push["occurred_at"])
        outgoing = earlier[earlier["direction"] == "out"]

        assert len(instance) == 1
        assert push["transaction_type"] == "transfer"
        # Nothing about the session is unusual: it is the victim acting.
        assert push["device_id"] in set(earlier["device_id"])
        assert push["channel"] in set(earlier["channel"])
        assert push["counterparty_wallet_id"] not in set(earlier["counterparty_wallet_id"].dropna())
        expected = min(params.drain_share.low * push["subject_balance_before"], limits[wallet])
        assert push["amount"] >= expected - 0.01
        if len(outgoing) >= 3:  # "unusually large" needs some history to be unusual against
            assert push["amount"] > outgoing["amount"].median()


@pytest.fixture(scope="module")
def market_dataset(small_config: GeneratorConfig) -> Dataset:
    """A mid-sized market at the default fraud rate, big enough for rates and mixes."""
    config = small_config.model_copy(
        update={
            "period": small_config.period.model_copy(update={"days": 30}),
            "population": small_config.population.model_copy(
                update={"wallets": 3000, "agents": 90, "merchants": 60}
            ),
        }
    )
    return generate(config, seed=21, key=KEY)


def test_fraud_rate_and_scenario_mix_match_the_config(
    market_dataset: Dataset, small_config: GeneratorConfig
):
    fraud = small_config.fraud
    rate = len(market_dataset.ground_truth) / len(market_dataset.transactions)
    mix = market_dataset.ground_truth["scenario"].value_counts(normalize=True)

    # This market ends before the drift day, so the new variant never appears.
    assert fraud.drift.day >= 30
    assert 0.5 * fraud.rate <= rate <= 1.5 * fraud.rate
    for scenario in FraudScenario:
        expected = 0 if scenario == fraud.drift.new_scenario else fraud.scenarios.share(scenario)
        assert abs(mix.get(scenario.value, 0) - expected) < 0.1, scenario


def test_every_instance_draws_its_own_parameters(fraud_dataset: Dataset):
    def per_instance(scenario: FraudScenario, measure) -> pd.Series:
        return _fraud(fraud_dataset, scenario).groupby("instance_id").apply(measure)

    def span_minutes(group: pd.DataFrame) -> float:
        return (group["occurred_at"].max() - group["occurred_at"].min()) / pd.Timedelta(minutes=1)

    def share_of_balance(group: pd.DataFrame) -> float:
        return float(group["amount"].iloc[0] / group["subject_balance_before"].iloc[0])

    varying = {
        FraudScenario.SIM_SWAP_TAKEOVER: per_instance(FraudScenario.SIM_SWAP_TAKEOVER, len),
        FraudScenario.ACCOUNT_TAKEOVER_DRAIN: per_instance(
            FraudScenario.ACCOUNT_TAKEOVER_DRAIN, span_minutes
        ),
        FraudScenario.MULE_FAN_IN: per_instance(FraudScenario.MULE_FAN_IN, len),
        FraudScenario.AGENT_STRUCTURING: per_instance(FraudScenario.AGENT_STRUCTURING, len),
        FraudScenario.SOCIAL_ENGINEERING_PUSH: per_instance(
            FraudScenario.SOCIAL_ENGINEERING_PUSH, share_of_balance
        ),
    }

    for scenario, values in varying.items():
        assert values.nunique() > 1, scenario


def test_a_tiny_market_still_generates_every_scenario(small_config: GeneratorConfig):
    tiny = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 40, "agents": 3, "merchants": 3}
            ),
            "fraud": small_config.fraud.model_copy(
                update={
                    "rate": 0.2,
                    "drift": small_config.fraud.drift.model_copy(update={"day": 10}),
                }
            ),
        }
    )

    dataset = generate(tiny, seed=1, key=KEY)

    assert set(dataset.ground_truth["scenario"]) == {s.value for s in FraudScenario}
