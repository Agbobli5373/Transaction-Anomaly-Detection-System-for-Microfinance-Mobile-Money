"""The model bundle: both models, the stacker and the thresholds, trained and shipped together."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from conftest import KEY
from scoring_helpers import RULES_CONFIG

from txrisk.evaluation import EvaluationConfig, Split, split_of
from txrisk.features import FeatureConfig, feature_table
from txrisk.generator import GeneratorConfig, generate
from txrisk.models import model_inputs
from txrisk.models.bundle import ModelBundle, train_bundle
from txrisk.scoring import load_rules_config, rule_scores_table, score_table

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
    features = market.features[market.features["transaction_id"].isin(transactions["transaction_id"])]
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
    pd.testing.assert_series_equal(loaded.risk_scores(inputs, rules), bundle.risk_scores(inputs, rules))


def test_the_same_training_gives_the_same_version(market: Market, bundle: ModelBundle):
    assert _train(market).version == bundle.version
