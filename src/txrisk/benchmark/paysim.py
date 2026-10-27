"""The PaySim benchmark: our scorers on a public synthetic mobile-money dataset we did not design.

PaySim has about 6.3M Transactions over 743 hourly steps, with its own fraud. It carries only part
of what our features and rules need, so this module maps what it has onto the shared Transaction
table and leaves the rest out explicitly, rather than inventing values for it. Everything after
the mapping is the shared code: the feature function, the rules, the models, the Alert budget
thresholds and the evaluation report.

What is mapped, and what is not (docs/evaluation/paysim.md has the reasoning):

- The five PaySim types become our Transaction types (`TYPE_MAPPING`).
- `step` is hours since the start, so step 1 is 00:00 of an arbitrary first day.
- `nameOrig` is the Subject wallet and `nameDest` the Counterparty.
- `oldbalanceOrg` is the Subject wallet's balance before the Transaction.
- `newbalanceOrig`, `newbalanceDest` and `oldbalanceDest` are never read: the file is opened with
  only the columns above, so they cannot reach a feature, a label or a score. `isFlaggedFraud` is
  PaySim's own rule output and is not read either.
- Regions, devices, KYC tiers, Agents, channels, SIM swaps and registration dates do not exist.
  The features that need them are emptied and the rules that need them are disabled.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from txrisk.evaluation import EvaluationConfig, Split, load_evaluation_config, split_of
from txrisk.evaluation.report import report_metrics
from txrisk.evaluation.scorers import anomaly_scores, stacked_scores, supervised_scores
from txrisk.features import FeatureConfig, feature_table, load_feature_config
from txrisk.scoring import RULES, RULES_ONLY, RulesConfig, load_rules_config, rule_scores_table

# PaySim has no calendar: step 1 is the first hour of this day. Only the hour of day and the
# distance between Transactions mean anything.
PERIOD_START = date(2026, 1, 1)
_HOUR = pd.Timedelta(hours=1)
UNKNOWN_TIER = "unknown"
SCORERS = [RULES_ONLY, "anomaly", "supervised", "stacked"]
SEED = 42

# The only PaySim columns ever read.
READ_COLUMNS = ["step", "type", "amount", "nameOrig", "oldbalanceOrg", "nameDest", "isFraud"]


@dataclass(frozen=True)
class TypeMap:
    transaction_type: str
    direction: str
    # Which Counterparty column PaySim's `nameDest` fills for this type.
    counterparty: str


TYPE_MAPPING = {
    # A customer pays cash in through a merchant, which PaySim calls the destination. Our Agent
    # features are emptied, so the destination is only the Counterparty here.
    "CASH_IN": TypeMap("cash_in", "in", "agent_id"),
    "CASH_OUT": TypeMap("cash_out", "out", "agent_id"),
    "TRANSFER": TypeMap("transfer", "out", "counterparty_wallet_id"),
    "PAYMENT": TypeMap("payment", "out", "merchant_id"),
    # Money out to a bank: the closest of our types is a Cash-out. It is not a Bill payment (the
    # glossary keeps "Debit" away from that), and PaySim has no fraud in it, so it moves no metric.
    "DEBIT": TypeMap("cash_out", "out", "agent_id"),
}

# Feature columns that need data PaySim does not have, and why. They are emptied after the shared
# feature function runs, so no model or rule ever sees a value computed from a placeholder.
UNSUPPORTED_FEATURES = {
    "wallet_age_days": "no registration dates",
    "new_region_30d": "no regions",
    "seconds_since_sim_swap": "no SIM swaps",
    "agent_cash_out_count_24h": "no Agents",
    "agent_cash_out_amount_24h": "no Agents",
    "agent_near_limit_cash_out_count_24h": "no Agents or KYC tiers",
    "device_age_seconds": "no devices",
    "amount_to_kyc_limit": "no KYC tiers",
}
# Model inputs outside the feature table that need data PaySim does not have. Always 0.
UNSUPPORTED_INPUTS = {"is_app": "no channel"}
UNSUPPORTED_RULES = {
    "SIM_SWAP_DRAIN": "no SIM swaps",
    "NEW_DEVICE_BURST": "no devices",
    "AGENT_STRUCTURING": "no Agents or KYC tiers",
    "NEAR_KYC_LIMIT": "no KYC tiers",
    "NEW_REGION": "no regions",
}
SUPPORTED_RULES = [code for code in RULES if code not in UNSUPPORTED_RULES]


@dataclass(frozen=True)
class PaySim:
    """PaySim as the shared pipeline sees it: the same tables the generator writes."""

    transactions: pd.DataFrame
    wallets: pd.DataFrame
    sim_swaps: pd.DataFrame
    # Ground truth: the type of each fraudulent Transaction, which stands in for a Fraud scenario.
    truth: pd.DataFrame
    # Complete labels: every fraud is known the moment it happens.
    discoveries: pd.DataFrame


def load_paysim(csv: Path) -> PaySim:
    """Map a PaySim CSV onto the shared tables. Only `READ_COLUMNS` are read from the file."""
    raw = pd.read_csv(
        csv,
        usecols=READ_COLUMNS,
        dtype={
            "step": "int64",
            "type": "string",
            "amount": "float64",
            "nameOrig": "string",
            "oldbalanceOrg": "float64",
            "nameDest": "string",
            "isFraud": "int8",
        },
    )
    unknown = sorted(set(raw["type"].dropna()) - set(TYPE_MAPPING))
    if unknown or raw["type"].isna().any():
        raise ValueError(f"PaySim types with no mapping: {unknown}")
    # Rows keep the file's order, which breaks ties between Transactions in the same hour.
    transaction_id = pd.Series(
        "paysim-" + pd.Series(np.arange(len(raw))).astype(str).str.zfill(8), dtype="string"
    )
    mapped = raw["type"].map(lambda t: TYPE_MAPPING[t].transaction_type).astype("string")
    transactions = pd.DataFrame(
        {
            "transaction_id": transaction_id,
            "occurred_at": pd.Timestamp(PERIOD_START, tz="UTC") + (raw["step"] - 1) * _HOUR,
            "transaction_type": mapped,
            "amount": raw["amount"],
            "subject_wallet_id": raw["nameOrig"],
            "direction": raw["type"].map(lambda t: TYPE_MAPPING[t].direction).astype("string"),
            "region": pd.Series("", index=raw.index, dtype="string"),
            "subject_balance_before": raw["oldbalanceOrg"],
            "device_id": pd.Series("", index=raw.index, dtype="string"),
            "channel": pd.Series("", index=raw.index, dtype="string"),
            "paysim_type": raw["type"],
        }
    )
    for column in ("counterparty_wallet_id", "agent_id", "merchant_id", "biller_id"):
        used = raw["type"].map(lambda t, c=column: TYPE_MAPPING[t].counterparty == c)
        transactions[column] = raw["nameDest"].where(used.astype(bool)).astype("string")
    wallets = pd.DataFrame(
        {"wallet_id": raw["nameOrig"].drop_duplicates().astype("string")}
    ).reset_index(drop=True)
    # Placeholders the shared replay needs; the features they feed are emptied.
    wallets["registered_at"] = pd.Timestamp(PERIOD_START, tz="UTC")
    wallets["kyc_tier"] = UNKNOWN_TIER
    sim_swaps = pd.DataFrame(
        {
            "wallet_id": pd.Series([], dtype="string"),
            "occurred_at": pd.Series([], dtype="datetime64[ns, UTC]"),
        }
    )
    fraud = raw["isFraud"].astype(bool)
    truth = pd.DataFrame(
        {"transaction_id": transaction_id[fraud], "scenario": raw.loc[fraud, "type"]}
    ).reset_index(drop=True)
    discoveries = pd.DataFrame(
        {
            "transaction_id": transaction_id[fraud],
            "discovered_at": transactions.loc[fraud, "occurred_at"],
        }
    ).reset_index(drop=True)
    return PaySim(transactions, wallets, sim_swaps, truth, discoveries)


def paysim_feature_config(path: Path) -> FeatureConfig:
    """The feature settings of `config/features.toml`, with one placeholder KYC tier."""
    return load_feature_config(path, kyc_limits={UNKNOWN_TIER: 1.0})


def paysim_features(paysim: PaySim, config: FeatureConfig) -> pd.DataFrame:
    """The shared feature table of PaySim, with every unsupported feature emptied."""
    table = feature_table(paysim.transactions, paysim.wallets, paysim.sim_swaps, config=config)
    for column in UNSUPPORTED_FEATURES:
        table[column] = pd.Series(pd.NA, index=table.index, dtype=table[column].dtype)
    return table


def paysim_rules(rules: RulesConfig) -> RulesConfig:
    """The rules with those PaySim cannot feed switched off. Their thresholds are unchanged."""
    return rules.model_copy(
        update={"rules": rules.rules.model_copy(update={"disabled": frozenset(UNSUPPORTED_RULES)})}
    )


def _rules_only_scores(
    transactions: pd.DataFrame,
    feature_rows: pd.DataFrame,
    rules: RulesConfig,
    evaluation: EvaluationConfig,
) -> pd.DataFrame:
    """The rules alone, with their own fixed Risk bands, from the feature table."""
    split = split_of(transactions["occurred_at"], PERIOD_START, evaluation)
    evaluated = transactions[(split == Split.EVALUATION).fillna(False).to_numpy(dtype=bool)]
    rule_scores = rule_scores_table(evaluated, feature_rows, rules)
    score = 100 * rule_scores["rules_score"].to_numpy(dtype=np.float64)
    score = np.where(
        rule_scores["critical"].to_numpy(dtype=bool), np.maximum(score, rules.critical_floor), score
    )
    risk_score = np.round(score)
    band = np.where(
        risk_score >= rules.bands.high,
        "High",
        np.where(risk_score >= rules.bands.medium, "Medium", "Low"),
    )
    return pd.DataFrame(
        {
            "transaction_id": rule_scores.index.to_numpy(),
            "risk_score": risk_score,
            "risk_band": band,
            "rules_score": rule_scores["rules_score"].to_numpy(),
        }
    ).astype({"transaction_id": "string", "risk_band": "string"})


def _scores(
    scorer: str,
    paysim: PaySim,
    feature_rows: pd.DataFrame,
    *,
    features: FeatureConfig,
    rules: RulesConfig,
    evaluation: EvaluationConfig,
) -> pd.DataFrame:
    common: dict[str, Any] = {
        "features": features,
        "evaluation": evaluation,
        "period_start": PERIOD_START,
        "feature_rows": feature_rows,
    }
    tables = (paysim.transactions, paysim.wallets, paysim.sim_swaps)
    match scorer:
        case "rules-only":
            return _rules_only_scores(paysim.transactions, feature_rows, rules, evaluation)
        case "anomaly":
            return anomaly_scores(*tables, **common)
        case "supervised":
            return supervised_scores(*tables, paysim.discoveries, **common)
        case "stacked":
            return stacked_scores(*tables, paysim.discoveries, rules=rules, **common)
    raise ValueError(f"unknown scorer {scorer!r}")


def _judge(
    scores: pd.DataFrame,
    paysim: PaySim,
    *,
    scorer: str,
    evaluation: EvaluationConfig,
    out: Path,
) -> dict[str, Any]:
    """The same metrics as the main evaluation, over PaySim's evaluation split."""
    transactions = paysim.transactions
    split = split_of(transactions["occurred_at"], PERIOD_START, evaluation)
    evaluated = transactions[(split == Split.EVALUATION).fillna(False).to_numpy(dtype=bool)]
    frame = evaluated.merge(scores, on="transaction_id", how="left").merge(
        paysim.truth, on="transaction_id", how="left"
    )
    if frame["risk_score"].isna().any():
        raise ValueError("every Transaction in the evaluation split needs a score")
    frame["fraud"] = frame["scenario"].notna()
    frame["day"] = frame["occurred_at"].dt.floor("D")
    start = pd.Timestamp(PERIOD_START, tz="UTC") + pd.Timedelta(
        days=evaluation.splits.evaluation.start
    )
    # PaySim ends before the last evaluation day does, so rates per day use the days it has.
    days = float((frame["occurred_at"].max() + _HOUR - start) / pd.Timedelta(days=1))
    fraud = frame["fraud"]
    extra = {
        "benchmark": {
            "dataset": "PaySim",
            "rows": len(transactions),
            "mapping": {k: v.transaction_type for k, v in TYPE_MAPPING.items()},
            "unsupported_features": UNSUPPORTED_FEATURES,
            "unsupported_inputs": UNSUPPORTED_INPUTS,
            "unsupported_rules": UNSUPPORTED_RULES,
            "supported_rules": SUPPORTED_RULES,
            "labels": "complete: every fraud is known when it happens",
            "evaluation_days_covered": days,
        }
    }
    return report_metrics(
        frame,
        scores,
        config=evaluation,
        scorer=scorer,
        out=out,
        days=days,
        discovered=fraud,
        eventually=fraud,
        segments=("paysim_type",),
        extra=extra,
    )


def run_paysim(
    csv: Path,
    *,
    out: Path,
    scorers: list[str],
    evaluation_config: Path,
    feature_config: Path,
    rules_config: Path,
    features_cache: Path | None = None,
) -> dict[str, dict[str, Any]]:
    """Judge each of `scorers` on PaySim and write its report to `out/<scorer>/`.

    The feature table is the slow part, so it is built once for all scorers, and read from or
    saved to `features_cache` when one is given.
    """
    evaluation = load_evaluation_config(evaluation_config)
    features = paysim_feature_config(feature_config)
    rules = paysim_rules(load_rules_config(rules_config))
    paysim = load_paysim(csv)
    if features_cache is not None and features_cache.exists():
        feature_rows = pd.read_parquet(features_cache)
    else:
        feature_rows = paysim_features(paysim, features)
        if features_cache is not None:
            features_cache.parent.mkdir(parents=True, exist_ok=True)
            feature_rows.to_parquet(features_cache, index=False)
    results: dict[str, dict[str, Any]] = {}
    for scorer in scorers:
        scores = _scores(
            scorer, paysim, feature_rows, features=features, rules=rules, evaluation=evaluation
        )
        results[scorer] = _judge(
            scores, paysim, scorer=scorer, evaluation=evaluation, out=out / scorer
        )
    return results
