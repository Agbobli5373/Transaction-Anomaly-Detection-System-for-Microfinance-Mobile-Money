"""Scorers the evaluation can judge, each giving Risk scores for the evaluation split."""

from datetime import date

import pandas as pd

from txrisk.evaluation.config import EvaluationConfig
from txrisk.evaluation.labels import Split, split_of
from txrisk.evaluation.thresholds import band, budget_thresholds
from txrisk.features import FeatureConfig, feature_table
from txrisk.models import model_inputs
from txrisk.models.anomaly import AnomalyModel
from txrisk.models.bundle import train_bundle
from txrisk.models.supervised import train_supervised
from txrisk.scoring import RulesConfig, rule_scores_table, score_table

ANOMALY_SEED = 42
SUPERVISED_SEED = 42
STACKED_SEED = 42


def rules_only_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    features: FeatureConfig,
    rules: RulesConfig,
    evaluation: EvaluationConfig,
    period_start: date,
) -> pd.DataFrame:
    """The rules alone, with their own fixed Risk bands."""
    start = pd.Timestamp(period_start, tz="UTC")
    return score_table(
        transactions,
        wallets,
        sim_swaps,
        features=features,
        rules=rules,
        score_from=start + pd.Timedelta(days=evaluation.splits.evaluation.start),
    )


def anomaly_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    features: FeatureConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    method: str = "isolation_forest",
    feature_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The Anomaly score alone: fitted on the training days with no labels, banded at the Alert
    budget on the threshold days, and scored on the evaluation days. The Risk score is the
    Anomaly score x 100.

    `feature_rows` is a feature table already built for these Transactions (`txrisk features`);
    without one it is built here, which is the slow part of the run.
    """
    if feature_rows is None:
        feature_rows = feature_table(transactions, wallets, sim_swaps, config=features)
    inputs = model_inputs(transactions, feature_rows)
    split = split_of(transactions["occurred_at"], period_start, evaluation)

    def rows_in(name: Split) -> pd.DataFrame:
        # Transactions outside every split have no split and are never in one.
        in_split: pd.DataFrame = inputs[(split == name).fillna(False).to_numpy(dtype=bool)]
        return in_split

    training = rows_in(Split.TRAIN)
    if method == "isolation_forest":
        model = AnomalyModel.fit(training, seed=ANOMALY_SEED)
    else:
        model = AnomalyModel.fit_lof(training, seed=ANOMALY_SEED)

    window = evaluation.splits.thresholds
    thresholds = budget_thresholds(
        model.score(rows_in(Split.THRESHOLDS)) * 100,
        days=window.end - window.start,
        budget_per_day=evaluation.alert_budget_per_day,
    )
    anomaly = model.score(rows_in(Split.EVALUATION))
    risk_score = anomaly * 100
    return pd.DataFrame(
        {
            "transaction_id": anomaly.index.to_numpy(),
            "risk_score": risk_score.to_numpy(),
            "risk_band": band(risk_score, thresholds).to_numpy(),
            "anomaly_score": anomaly.to_numpy(),
        }
    ).astype({"transaction_id": "string", "risk_band": "string"})


def supervised_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    discoveries: pd.DataFrame,
    *,
    features: FeatureConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    feature_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The supervised fraud probability alone: trained and calibrated on the Fraud labels known
    when the evaluation days start, banded at the Alert budget on the threshold days, and scored
    on the evaluation days. The Risk score is the calibrated probability x 100; the raw
    probability comes along for the reliability check.

    `feature_rows` is a feature table already built for these Transactions (`txrisk features`);
    without one it is built here, which is the slow part of the run.
    """
    if feature_rows is None:
        feature_rows = feature_table(transactions, wallets, sim_swaps, config=features)
    model = train_supervised(
        transactions,
        feature_rows,
        discoveries,
        period_start=period_start,
        config=evaluation,
        seed=SUPERVISED_SEED,
    )
    inputs = model_inputs(transactions, feature_rows)
    split = split_of(transactions["occurred_at"], period_start, evaluation)

    def rows_in(name: Split) -> pd.DataFrame:
        in_split: pd.DataFrame = inputs[(split == name).fillna(False).to_numpy(dtype=bool)]
        return in_split

    window = evaluation.splits.thresholds
    thresholds = budget_thresholds(
        model.score(rows_in(Split.THRESHOLDS)) * 100,
        days=window.end - window.start,
        budget_per_day=evaluation.alert_budget_per_day,
    )
    evaluated = rows_in(Split.EVALUATION)
    probability = model.score(evaluated)
    risk_score = probability * 100
    return pd.DataFrame(
        {
            "transaction_id": probability.index.to_numpy(),
            "risk_score": risk_score.to_numpy(),
            "risk_band": band(risk_score, thresholds).to_numpy(),
            "fraud_probability": probability.to_numpy(),
            "raw_probability": model.raw_probability(evaluated).to_numpy(),
        }
    ).astype({"transaction_id": "string", "risk_band": "string"})


def stacked_scores(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    discoveries: pd.DataFrame,
    *,
    features: FeatureConfig,
    rules: RulesConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    feature_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """The stacked Risk score: a model bundle trained as `txrisk train` trains it, banded at its
    own thresholds, and scored on the evaluation days.

    Each component comes along, and so does the stacker's probability before the Critical rule
    floor, for the reliability check. The scores come from the feature table rather than a
    replay; the scoring service gives the same ones.
    """
    if feature_rows is None:
        feature_rows = feature_table(transactions, wallets, sim_swaps, config=features)
    rule_scores = rule_scores_table(transactions, feature_rows, rules)
    bundle = train_bundle(
        transactions,
        feature_rows,
        discoveries,
        rules=rules,
        period_start=period_start,
        config=evaluation,
        seed=STACKED_SEED,
        rule_scores=rule_scores,
    )
    split = split_of(transactions["occurred_at"], period_start, evaluation)
    evaluated = transactions[(split == Split.EVALUATION).fillna(False).to_numpy(dtype=bool)]
    inputs = model_inputs(evaluated, feature_rows)
    rule_scores = rule_scores.loc[inputs.index]
    components = bundle.components(inputs, rule_scores)
    risk_score = bundle.risk_scores(inputs, rule_scores, components)
    return pd.DataFrame(
        {
            "transaction_id": inputs.index.to_numpy(),
            "risk_score": risk_score.to_numpy(),
            "risk_band": bundle.bands(
                risk_score,
                rule_scores,
                feature_rows.set_index("transaction_id")["market_spend_index"]
                .reindex(inputs.index)
                .astype("float64"),
            ).to_numpy(),
            "fraud_probability": (bundle.stacker.score(components) / 100).to_numpy(),
            "rules_score": rule_scores["rules_score"].to_numpy(),
            "critical": rule_scores["critical"].to_numpy(),
            "anomaly_score": components["anomaly"].to_numpy(),
            "supervised_probability": components["fraud_probability"].to_numpy(),
            # The bundle `txrisk train` gives for the same data and seed has this version too.
            "model_version": bundle.version,
        }
    ).astype({"transaction_id": "string", "risk_band": "string", "model_version": "string"})
