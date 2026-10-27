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
from txrisk.models.anomaly import AnomalyModel
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


def test_calibration_learns_from_the_calibration_split_and_training_does_not(market: Market):
    # Legitimate-looking calibration-split Transfers, reported as fraud before day 14.
    tx = market.transactions
    quiet = tx[
        (tx["occurred_at"] >= _at(8))
        & (tx["occurred_at"] < _at(11))
        & (tx["transaction_type"] == "transfer")
        & ~tx["transaction_id"].isin(market.discoveries["transaction_id"])
    ].nlargest(40, "amount")["transaction_id"]
    model = _train(market)

    relabelled = _train(market, _discover(market, quiet, _at(13)))

    calibrated = model.score(market.inputs)
    assert calibrated.between(0, 1).all()
    pd.testing.assert_series_equal(
        relabelled.raw_probability(market.inputs), model.raw_probability(market.inputs)
    )
    assert not relabelled.score(market.inputs).equals(calibrated)


def test_calibration_fits_the_calibration_labels_better_than_the_raw_probability(market: Market):
    tx = market.transactions
    calibration = tx.loc[
        (tx["occurred_at"] >= _at(8)) & (tx["occurred_at"] < _at(11)), "transaction_id"
    ]
    found = market.discoveries.loc[market.discoveries["discovered_at"] < _at(14), "transaction_id"]
    fraud = calibration.isin(found).to_numpy(dtype=float)
    model = _train(market)
    inputs = market.inputs.loc[calibration]

    def brier(probability: pd.Series) -> float:
        return float(((probability.to_numpy() - fraud) ** 2).mean())

    # Class weighting tilts the raw probability towards fraud; calibration is fitted to these
    # labels and brings it back to how often they say fraud happens.
    assert brier(model.score(inputs)) < brier(model.raw_probability(inputs))


def test_calibration_keeps_the_model_s_ranking(market: Market):
    model = _train(market)
    raw = model.raw_probability(market.inputs)
    calibrated = model.score(market.inputs)
    # Above the highest calibration point the probability is flat; below it, it never is.
    inside = calibrated < calibrated.max()

    ranked = calibrated[raw[inside].sort_values(kind="stable").index]

    # A step function would put whole bands of Transactions on one probability, and an Alert
    # budget threshold can only take or leave a step whole.
    assert ranked.is_monotonic_increasing
    assert ranked.nunique() == raw[inside].nunique()


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


def test_transactions_outside_every_split_change_nothing(market: Market):
    # The splits stop at day 20 while the data runs to day 21, as when a Fraud scenario that
    # starts near the end of the period runs past it.
    short = CONFIG.model_copy(
        update={
            "splits": CONFIG.splits.model_copy(
                update={"evaluation": CONFIG.splits.evaluation.model_copy(update={"end": 20})}
            )
        }
    )

    model = train_supervised(
        market.transactions,
        market.features,
        market.discoveries,
        period_start=START,
        config=short,
        seed=7,
    )

    pd.testing.assert_series_equal(model.score(market.inputs), _train(market).score(market.inputs))


def _assess(market: Market, transaction_id: str, model=None, anomaly=None):
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
                anomaly=anomaly,
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


def test_the_fraud_probability_and_the_anomaly_score_are_reported_side_by_side(market: Market):
    model = _train(market)
    anomaly = AnomalyModel.fit(market.inputs, seed=7)
    tx = market.transactions
    scored = tx.loc[tx["occurred_at"] >= _at(14), "transaction_id"].iloc[-1]

    both = _assess(market, scored, model, anomaly)

    assert both.component_scores["supervised"] == pytest.approx(
        model.score(market.inputs.loc[[scored]]).iloc[0]
    )
    assert both.component_scores["anomaly"] == pytest.approx(
        anomaly.score(market.inputs.loc[[scored]]).iloc[0]
    )
