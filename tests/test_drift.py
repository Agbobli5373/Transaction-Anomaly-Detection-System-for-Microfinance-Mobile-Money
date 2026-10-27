import pandas as pd
import pytest
from conftest import KEY

from txrisk.generator import Dataset, FraudScenario, GeneratorConfig, generate

DRIFT_DAY = 10


@pytest.fixture(scope="module")
def config(small_config: GeneratorConfig) -> GeneratorConfig:
    """Twenty-one days with drift on day 10 and plenty of fraud on both sides of it."""
    fraud = small_config.fraud
    return small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 1500, "agents": 60, "merchants": 30}
            ),
            "fraud": fraud.model_copy(
                update={"rate": 0.03, "drift": fraud.drift.model_copy(update={"day": DRIFT_DAY})}
            ),
        }
    )


@pytest.fixture(scope="module")
def dataset(config: GeneratorConfig) -> Dataset:
    return generate(config, seed=17, key=KEY)


def _drift_moment(config: GeneratorConfig) -> pd.Timestamp:
    return pd.Timestamp(config.period.start, tz="UTC") + pd.Timedelta(days=DRIFT_DAY)


def _fraud(dataset: Dataset, scenario: FraudScenario) -> pd.DataFrame:
    truth = dataset.ground_truth[dataset.ground_truth["scenario"] == scenario.value]
    return truth.merge(dataset.transactions, on="transaction_id").sort_values("occurred_at")


def test_the_default_market_drifts_on_day_100(default_config: GeneratorConfig):
    assert default_config.fraud.drift.day == 100
    assert default_config.fraud.drift.new_scenario == FraudScenario.REVERSAL_SCAM


def test_the_new_variant_appears_only_after_the_drift_day(
    dataset: Dataset, config: GeneratorConfig
):
    scams = _fraud(dataset, FraudScenario.REVERSAL_SCAM)

    assert scams["instance_id"].nunique() >= 3
    assert (scams["occurred_at"] >= _drift_moment(config)).all()


def test_a_reversal_scam_baits_with_a_small_transfer_and_takes_back_more(
    dataset: Dataset, config: GeneratorConfig
):
    params = config.fraud.scenarios.reversal_scam
    tx = dataset.transactions
    returned_multiple: list[float] = []

    for _, scam in _fraud(dataset, FraudScenario.REVERSAL_SCAM).groupby("instance_id"):
        if len(scam) < 2:  # the victim had nothing to send back
            continue
        bait, back = scam.iloc[0], scam.iloc[1]
        victim, fraudster = bait["counterparty_wallet_id"], bait["subject_wallet_id"]
        before = tx[(tx["subject_wallet_id"] == victim) & (tx["occurred_at"] < bait["occurred_at"])]

        assert bait["transaction_type"] == back["transaction_type"] == "transfer"
        assert params.bait_amount.low <= bait["amount"] <= params.bait_amount.high
        assert back["subject_wallet_id"] == victim and back["counterparty_wallet_id"] == fraudster
        assert back["occurred_at"] - bait["occurred_at"] <= pd.Timedelta(
            minutes=params.reply_minutes.high
        )
        # A victim short of money can only send back what they hold, but never less than the bait.
        assert back["amount"] >= bait["amount"]
        assert fraudster not in set(before["counterparty_wallet_id"].dropna())
        returned_multiple.append(back["amount"] / bait["amount"])

    assert pd.Series(returned_multiple).median() >= params.return_multiple.low


def test_sim_swap_takeovers_change_shape_from_the_drift_day(
    dataset: Dataset, config: GeneratorConfig
):
    original = config.fraud.scenarios.sim_swap_takeover
    shifted = config.fraud.drift.sim_swap_takeover
    assert shifted is not None
    swaps = dataset.sim_swaps
    drift = _drift_moment(config)
    seen = {"before": 0, "after": 0}

    for _, instance in _fraud(dataset, FraudScenario.SIM_SWAP_TAKEOVER).groupby("instance_id"):
        first = instance.iloc[0]
        swapped = swaps.loc[
            (swaps["wallet_id"] == first["subject_wallet_id"])
            & (swaps["occurred_at"] <= first["occurred_at"]),
            "occurred_at",
        ].max()
        delay_hours = (first["occurred_at"] - swapped) / pd.Timedelta(hours=1)
        params, side = (shifted, "after") if swapped >= drift else (original, "before")
        seen[side] += 1
        assert params.delay_hours.low <= delay_hours <= params.delay_hours.high, side
        assert len(instance) <= params.transactions.high, side

    assert seen["before"] >= 3 and seen["after"] >= 3


def test_no_transaction_runs_past_the_end_of_the_period(small_config: GeneratorConfig):
    # Drift near the end: shifted SIM-swap takeovers wait up to 72 hours after the SIM swap,
    # so instances that start in the last days would otherwise run past the period.
    fraud = small_config.fraud
    late_drift = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(update={"wallets": 1500}),
            "fraud": fraud.model_copy(
                update={"rate": 0.05, "drift": fraud.drift.model_copy(update={"day": 15})}
            ),
        }
    )

    dataset = generate(late_drift, seed=3, key=KEY)

    end = pd.Timestamp(late_drift.period.start, tz="UTC") + pd.Timedelta(
        days=late_drift.period.days
    )
    assert (dataset.transactions["occurred_at"] < end).all()
    assert (dataset.sim_swaps["occurred_at"] < end).all()
