"""The supervised fraud probability: trained on discovered Fraud labels, calibrated, seeded."""

from dataclasses import dataclass
from datetime import date

import pandas as pd
import pytest
from conftest import KEY
from scoring_helpers import RULES_CONFIG

from txrisk.evaluation import EvaluationConfig
from txrisk.features import FeatureConfig, feature_table, replay_history, transactions_from_frame
from txrisk.generator import GeneratorConfig, generate
from txrisk.models import model_inputs
from txrisk.models.supervised import train_supervised
from txrisk.scoring import FixedClock, ScoringService, load_rules_config

START = date(2026, 1, 1)
# Train on days 0-8, calibrate on 8-11; the model goes live on day 14, when only days before 11
# have mature labels.
CONFIG = EvaluationConfig.model_validate(
    {
        "label_maturity_days": 3,
        "alert_budget_per_day": 20,
        "wallet_age_band_edges": [30, 180],
        "splits": {
            "train": [0, 8],
            "calibration": [8, 11],
            "thresholds": [11, 14],
            "evaluation": [14, 21],
        },
    }
)


def _at(day: float) -> pd.Timestamp:
    return pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=day)


@dataclass(frozen=True)
class Market:
    config: GeneratorConfig
    wallets: pd.DataFrame
    sim_swaps: pd.DataFrame
    transactions: pd.DataFrame
    features: pd.DataFrame
    discoveries: pd.DataFrame
    inputs: pd.DataFrame


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    """21 days of a market with plenty of fraud, so the train split has labels to learn from."""
    config = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 800, "agents": 40, "merchants": 20}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )
    dataset = generate(config, seed=4, key=KEY)
    limits = {tier.name: tier.single_transaction_limit for tier in config.market.kyc_tiers}
    features = feature_table(
        dataset.transactions,
        dataset.wallets,
        dataset.sim_swaps,
        config=FeatureConfig(kyc_limits=limits),
    )
    return Market(
        config=config,
        wallets=dataset.wallets,
        sim_swaps=dataset.sim_swaps,
        transactions=dataset.transactions,
        features=features,
        discoveries=dataset.fraud_discoveries,
        inputs=model_inputs(dataset.transactions, features),
    )


def _train(market: Market, discoveries: pd.DataFrame | None = None, seed: int = 7):
    return train_supervised(
        market.transactions,
        market.features,
        market.discoveries if discoveries is None else discoveries,
        period_start=START,
        config=CONFIG,
        seed=seed,
    )


def test_the_same_seed_gives_the_same_probabilities(market: Market):
    first = _train(market).score(market.inputs)
    second = _train(market).score(market.inputs)

    pd.testing.assert_series_equal(first, second)


def _discover(market: Market, transaction_ids: pd.Series, at: pd.Timestamp) -> pd.DataFrame:
    """The market's Fraud discoveries plus these Transactions, discovered at `at`."""
    extra = pd.DataFrame({"transaction_id": transaction_ids, "discovered_at": at})
    return pd.concat([market.discoveries, extra.astype(market.discoveries.dtypes[extra.columns])])


def test_fraud_discovered_after_the_model_goes_live_is_not_learned_as_fraud(market: Market):
    # Large train-split Transfers nobody has reported. The model goes live on day 14.
    tx = market.transactions
    quiet = tx[
        (tx["occurred_at"] < _at(8))
        & (tx["transaction_type"] == "transfer")
        & ~tx["transaction_id"].isin(market.discoveries["transaction_id"])
    ].nlargest(40, "amount")["transaction_id"]
    baseline = _train(market).score(market.inputs)

    found_late = _train(market, _discover(market, quiet, _at(14))).score(market.inputs)
    found_early = _train(market, _discover(market, quiet, _at(13.9))).score(market.inputs)

    pd.testing.assert_series_equal(found_late, baseline)
    assert not found_early.equals(baseline)


def test_probabilities_are_calibrated_to_the_fraud_found_in_the_calibration_split(market: Market):
    tx = market.transactions
    calibration = tx[(tx["occurred_at"] >= _at(8)) & (tx["occurred_at"] < _at(11))]
    found = market.discoveries.loc[market.discoveries["discovered_at"] < _at(14), "transaction_id"]
    fraud_share = calibration["transaction_id"].isin(found).mean()
    model = _train(market)

    calibrated = model.score(market.inputs)
    raw = model.raw_probability(market.inputs)

    assert calibrated.between(0, 1).all()
    # Isotonic calibration keeps the average of what it was fitted on, and only on that.
    assert calibrated[calibration["transaction_id"]].mean() == pytest.approx(fraud_share)
    assert raw[calibration["transaction_id"]].mean() != pytest.approx(fraud_share, rel=0.1)


def test_nothing_after_the_calibration_split_shapes_the_model(market: Market):
    tx = market.transactions
    early = tx["occurred_at"] < _at(11)
    model = _train(market)

    cut_short = train_supervised(
        tx[early],
        market.features[early],
        market.discoveries,
        period_start=START,
        config=CONFIG,
        seed=7,
    )

    pd.testing.assert_series_equal(cut_short.score(market.inputs), model.score(market.inputs))


def _assess(market: Market, transaction_id: str, model=None):
    """Assess one Transaction with every earlier Transaction already in the history."""
    limits = {tier.name: tier.single_transaction_limit for tier in market.config.market.kyc_tiers}
    history = replay_history(market.wallets, market.sim_swaps)
    chronological = market.transactions.sort_values("occurred_at", kind="stable")
    for transaction in transactions_from_frame(chronological):
        if transaction.transaction_id == transaction_id:
            service = ScoringService(
                history,
                FixedClock(transaction.occurred_at),
                features=FeatureConfig(kyc_limits=limits),
                rules=load_rules_config(RULES_CONFIG),
                supervised=model,
            )
            return service.assess(transaction)
        history.record(transaction)
    raise KeyError(transaction_id)


def test_the_scoring_service_reports_the_fraud_probability_as_a_component(market: Market):
    model = _train(market)
    batch = model.score(market.inputs)
    tx = market.transactions
    # The evaluation-day Transaction the model finds most likely to be fraud.
    riskiest = batch[tx.loc[tx["occurred_at"] >= _at(14), "transaction_id"]].idxmax()

    with_model = _assess(market, riskiest, model)
    rules_only = _assess(market, riskiest)

    # Scored one at a time it gets the probability training-time inputs would give it.
    assert with_model.component_scores["supervised"] == pytest.approx(batch[riskiest])
    assert with_model.component_scores["supervised"] > 0
    assert rules_only.component_scores["supervised"] is None
    # Combining the components is a later step: the rules still decide the Risk score.
    assert (with_model.risk_score, with_model.risk_band, with_model.reasons) == (
        rules_only.risk_score,
        rules_only.risk_band,
        rules_only.reasons,
    )
