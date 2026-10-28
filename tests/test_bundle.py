"""The model bundle: both models, the stacker and the thresholds, trained and shipped together."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from market_helpers import CONFIG, RULES, START, Market, build_market, replay_assessments, train

from txrisk.evaluation import Split, split_of
from txrisk.evaluation.thresholds import band
from txrisk.generator import GeneratorConfig
from txrisk.models import model_inputs
from txrisk.models.bundle import ModelBundle
from txrisk.models.stacked import STACK_COLUMNS
from txrisk.scoring import RiskAssessment, rule_scores_table, score_table


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return train(market)


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


def test_rule_scores_ignore_the_usual_amount_in_a_wallets_first_30_days(market: Market):
    inputs = model_inputs(market.transactions, market.features)
    codes = rule_scores_table(market.transactions, market.features, RULES)["rule_codes"]
    young = inputs["wallet_age_days"].to_numpy() < 30
    outgoing = (market.transactions["direction"] == "out").to_numpy()
    large = inputs["amount_to_median_30d"] > RULES.rules.AMOUNT_VS_MEDIAN.above_median_multiple

    assert (young & outgoing & large.to_numpy()).any()
    usual_amount = codes.str.contains("AMOUNT_VS_MEDIAN|FIRST_TIME_LARGE_TRANSFER", regex=True)
    assert not usual_amount[young].any()
    assert usual_amount[~young].any()


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
    assert train(market).version == bundle.version


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
    return scores, replay_assessments(market, bundle, ids)


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


def test_the_scoring_service_bands_a_busier_market_as_the_offline_table_does(market: Market):
    # A steep elasticity, so that the small market's ordinary swings in spending move High.
    bundle = train(market, CONFIG.model_copy(update={"surge_elasticity": 6.0}))
    transactions, features, rules = _rows(market, Split.EVALUATION)
    scores = bundle.risk_scores(model_inputs(transactions, features), rules)
    index = features.set_index("transaction_id")["market_spend_index"].astype(float)
    ids = list(scores.nlargest(80).index)

    offline = bundle.bands(scores, rules, index.reindex(scores.index))[ids]
    served = replay_assessments(market, bundle, set(ids))

    assert [served[i].risk_band.value for i in ids] == offline.tolist()
    # The market spent more than usual for some of these, and High moved with it.
    assert (offline != band(scores, bundle.thresholds)[ids]).any()
    assert (offline == "High").any()


def test_a_critical_rule_hit_is_high_however_busy_the_market(market: Market):
    bundle = train(market, CONFIG.model_copy(update={"surge_elasticity": 6.0}))
    transactions, features, rules = _rows(market, Split.EVALUATION)
    critical = rules.assign(critical=True)
    scores = bundle.risk_scores(model_inputs(transactions, features), critical)
    busiest = pd.Series(10.0, index=scores.index)

    assert (bundle.bands(scores, critical, busiest) == "High").all()
