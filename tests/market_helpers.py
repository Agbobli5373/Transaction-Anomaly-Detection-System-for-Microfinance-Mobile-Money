"""A small generated market with a trained model bundle, and how the scoring service serves it."""

from dataclasses import dataclass
from datetime import date

import pandas as pd
from conftest import KEY
from scoring_helpers import RULES_CONFIG

from txrisk.evaluation import EvaluationConfig
from txrisk.features import FeatureConfig, feature_table, replay_history, transactions_from_frame
from txrisk.generator import Dataset, GeneratorConfig, generate
from txrisk.models.bundle import ModelBundle, train_bundle
from txrisk.scoring import FixedClock, RiskAssessment, ScoringService, load_rules_config

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
    dataset: Dataset


def build_market(small_config: GeneratorConfig) -> Market:
    """21 days of 800 Wallets, with fraud common enough to train on."""
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
        dataset=dataset,
    )


def train(market: Market, config: EvaluationConfig = CONFIG) -> ModelBundle:
    return train_bundle(
        market.transactions,
        market.features,
        market.discoveries,
        rules=RULES,
        period_start=START,
        config=config,
        seed=11,
    )


def replay_assessments(
    market: Market, bundle: ModelBundle, ids: set[str]
) -> dict[str, RiskAssessment]:
    """The scoring service's assessment of each of `ids` over the offline replay, each with every
    earlier Transaction already in the history."""
    history = replay_history(market.wallets, market.sim_swaps)
    assessments = {}
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
            assessments[transaction.transaction_id] = service.assess(transaction)
        history.record(transaction)
    return assessments
