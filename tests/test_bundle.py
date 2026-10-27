"""The model bundle: both models, the stacker and the thresholds, trained and shipped together."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from conftest import KEY
from scoring_helpers import RULES_CONFIG

from txrisk.evaluation import EvaluationConfig, Split, split_of
from txrisk.evaluation.thresholds import band
from txrisk.features import FeatureConfig, feature_table, replay_history, transactions_from_frame
from txrisk.generator import GeneratorConfig, generate
from txrisk.models import model_inputs
from txrisk.models.bundle import ModelBundle, train_bundle
from txrisk.models.stacked import STACK_COLUMNS
from txrisk.scoring import (
    FixedClock,
    RiskAssessment,
    ScoringService,
    load_rules_config,
    rule_scores_table,
    score_table,
)

START = date(2026, 1, 1)
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
RULES = load_rules_config(RULES_CONFIG)


@dataclass(frozen=True)
class Market:
    transactions: pd.DataFrame
    wallets: pd.DataFrame
    sim_swaps: pd.DataFrame
    discoveries: pd.DataFrame
    features: pd.DataFrame
    feature_config: FeatureConfig


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    config = small_config.model_copy(
        update={
            "population": small_config.population.model_copy(
                update={"wallets": 800, "agents": 40, "merchants": 20}
            ),
            "fraud": small_config.fraud.model_copy(update={"rate": 0.03}),
        }
    )
    dataset = generate(config, seed=4, key=KEY)
    feature_config = FeatureConfig(
        kyc_limits={tier.name: tier.single_transaction_limit for tier in config.market.kyc_tiers}
    )
    return Market(
        transactions=dataset.transactions,
        wallets=dataset.wallets,
        sim_swaps=dataset.sim_swaps,
        discoveries=dataset.fraud_discoveries,
        features=feature_table(
            dataset.transactions, dataset.wallets, dataset.sim_swaps, config=feature_config
        ),
        feature_config=feature_config,
    )


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return _train(market)


def _train(market: Market) -> ModelBundle:
    return train_bundle(
        market.transactions,
        market.features,
        market.discoveries,
        rules=RULES,
        period_start=START,
        config=CONFIG,
        seed=11,
    )


def _rows(market: Market, name: Split) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    split = split_of(market.transactions["occurred_at"], START, CONFIG)
    rows = (split == name).fillna(False).to_numpy(dtype=bool)
    transactions = market.transactions[rows]
    features = market.features[
        market.features["transaction_id"].isin(transactions["transaction_id"])
    ]
    return transactions, features, rule_scores_table(transactions, features, RULES)


def test_rule_scores_from_a_feature_table_match_the_scoring_service(market: Market):
    replayed = score_table(
        market.transactions,
        market.wallets,
        market.sim_swaps,
        features=market.feature_config,
        rules=RULES,
    )

    from_table = rule_scores_table(market.transactions, market.features, RULES)

    pd.testing.assert_series_equal(
        from_table["rules_score"].reset_index(drop=True),
        replayed["rules_score"].astype(float).reset_index(drop=True),
        check_names=False,
    )


def test_the_bundle_records_which_days_fitted_what(bundle: ModelBundle):
    assert bundle.fitted_on == {
        "models": [0, 8],
        "stacker": [8, 11],
        "thresholds": [11, 14],
        "labels_known_on_day": 14,
    }


def test_on_the_threshold_days_high_is_the_alert_budget(market: Market, bundle: ModelBundle):
    transactions, features, rules = _rows(market, Split.THRESHOLDS)

    scores = bundle.risk_scores(model_inputs(transactions, features), rules)

    # 3 days at 20 a day, Critical rule hits included, give or take ties on the threshold.
    assert 60 <= (scores >= bundle.thresholds.high).sum() <= 62
    assert bundle.thresholds.medium < bundle.thresholds.high


def test_a_critical_rule_hit_is_always_high(market: Market, bundle: ModelBundle):
    transactions, features, rules = _rows(market, Split.EVALUATION)
    critical = rules.assign(critical=True)

    scores = bundle.risk_scores(model_inputs(transactions, features), critical)

    assert (scores >= bundle.thresholds.high).all()


def test_a_saved_bundle_loads_back_to_identical_scores(
    market: Market, bundle: ModelBundle, tmp_path: Path
):
    transactions, features, rules = _rows(market, Split.EVALUATION)
    inputs = model_inputs(transactions, features)

    bundle.save(tmp_path / "bundle")
    loaded = ModelBundle.load(tmp_path / "bundle")

    assert loaded.version == bundle.version
    pd.testing.assert_series_equal(
        loaded.risk_scores(inputs, rules), bundle.risk_scores(inputs, rules)
    )


def test_the_same_training_gives_the_same_version(market: Market, bundle: ModelBundle):
    assert _train(market).version == bundle.version


def test_input_contributions_add_up_to_the_models_share_of_the_risk_score(
    market: Market, bundle: ModelBundle
):
    transactions, features, rules = _rows(market, Split.EVALUATION)
    inputs = model_inputs(transactions, features)
    components = bundle.components(inputs, rules)
    weights = bundle.stacker.weights
    log_odds = bundle.stacker.intercept + sum(weights[c] * components[c] for c in STACK_COLUMNS)

    contributions = bundle.contributions(inputs)

    assert list(contributions.columns) == bundle.input_columns
    # Where the raw probability is clipped its log-odds no longer follow the trees.
    inside = components["raw_probability"].between(1e-6, 1 - 1e-6)
    models = log_odds - weights["rules"] * components["rules"]
    unexplained = (models - contributions.sum(axis=1))[inside]
    assert np.ptp(unexplained.to_numpy()) < 1e-6


def _served(market: Market, bundle: ModelBundle, ids: set[str]) -> dict[str, RiskAssessment]:
    """The scoring service's assessment of each of `ids`, each with every earlier Transaction
    already in the history."""
    history = replay_history(market.wallets, market.sim_swaps)
    served = {}
    chronological = market.transactions.sort_values("occurred_at", kind="stable")
    for transaction in transactions_from_frame(chronological):
        if transaction.transaction_id in ids:
            service = ScoringService(
                history,
                FixedClock(transaction.occurred_at),
                features=market.feature_config,
                rules=RULES,
                bundle=bundle,
            )
            served[transaction.transaction_id] = service.assess(transaction)
        history.record(transaction)
    return served


@pytest.fixture(scope="module")
def served(market: Market, bundle: ModelBundle) -> tuple[pd.Series, dict[str, RiskAssessment]]:
    """Batch Risk scores of the evaluation days, and the service's assessment of the riskiest
    ten, every Critical rule hit and ten others."""
    transactions, features, rules = _rows(market, Split.EVALUATION)
    scores = bundle.risk_scores(model_inputs(transactions, features), rules)
    ids = {
        *scores.nlargest(10).index,
        *rules.index[rules["critical"]],
        *scores.sample(10, random_state=1).index,
    }
    return scores, _served(market, bundle, ids)


def test_the_scoring_service_gives_the_bundle_s_risk_score_and_band(
    bundle: ModelBundle, served: tuple[pd.Series, dict[str, RiskAssessment]]
):
    scores, assessments = served
    expected = scores[list(assessments)]

    # Mostly well under 1, so the Risk score keeps two decimals rather than rounding to 0.
    assert [a.risk_score for a in assessments.values()] == pytest.approx(
        expected.tolist(), abs=0.005
    )
    assert [a.risk_band.value for a in assessments.values()] == band(
        expected, bundle.thresholds
    ).tolist()
    assert {a.model_version for a in assessments.values()} == {bundle.version}


def test_the_scoring_service_reports_every_component(
    market: Market, bundle: ModelBundle, served: tuple[pd.Series, dict[str, RiskAssessment]]
):
    _, assessments = served
    transactions, features, rules = _rows(market, Split.EVALUATION)
    inputs = model_inputs(transactions, features)
    components = bundle.components(inputs, rules)

    for transaction_id, assessment in assessments.items():
        reported = assessment.component_scores
        assert reported["rules"] == pytest.approx(rules.loc[transaction_id, "rules_score"])
        assert reported["anomaly"] == pytest.approx(components.loc[transaction_id, "anomaly"])
        assert reported["supervised"] == pytest.approx(
            components.loc[transaction_id, "fraud_probability"]
        )


def test_the_riskiest_transactions_come_with_distinct_reasons_critical_hits_first(
    market: Market, served: tuple[pd.Series, dict[str, RiskAssessment]]
):
    scores, assessments = served
    _, _, rules = _rows(market, Split.EVALUATION)

    for transaction_id in scores.nlargest(10).index:
        codes = [r.code for r in assessments[transaction_id].reasons]
        assert 1 <= len(codes) <= RULES.max_reasons
        assert len(set(codes)) == len(codes)
    for transaction_id in rules.index[rules["critical"]]:
        assert assessments[transaction_id].risk_band.value == "High"
        assert assessments[transaction_id].reasons[0].code == "SIM_SWAP_DRAIN"
