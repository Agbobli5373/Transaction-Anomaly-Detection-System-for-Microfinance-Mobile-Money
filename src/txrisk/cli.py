"""Command-line entry point: `txrisk <command>`."""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from txrisk.evaluation import load_evaluation_config
from txrisk.evaluation.report import evaluate
from txrisk.evaluation.scorers import (
    STACKED_SEED,
    anomaly_scores,
    rules_only_scores,
    stacked_scores,
    supervised_scores,
)
from txrisk.features import FeatureConfig, feature_table, load_feature_config
from txrisk.generator import generate, load_config, write_dataset
from txrisk.models.bundle import train_bundle
from txrisk.models.registry import MODEL_NAME, log_bundle
from txrisk.scoring import RULES_ONLY, load_rules_config

PSEUDONYM_KEY_ENV = "TXRISK_PSEUDONYM_KEY"


def _generate(args: argparse.Namespace) -> int:
    key = os.environ.get(PSEUDONYM_KEY_ENV)
    if not key:
        print(
            f"error: set {PSEUDONYM_KEY_ENV} to the secret used to pseudonymise MSISDNs",
            file=sys.stderr,
        )
        return 2
    dataset = generate(load_config(args.config), seed=args.seed, key=key.encode())
    write_dataset(dataset, args.out)
    print(f"wrote {len(dataset.transactions):,} transactions to {args.out}")
    return 0


def _features(args: argparse.Namespace) -> int:
    market = load_config(args.config).market
    config = load_feature_config(
        args.feature_config,
        kyc_limits={tier.name: tier.single_transaction_limit for tier in market.kyc_tiers},
    )
    table = feature_table(
        pd.read_parquet(args.data / "transactions.parquet"),
        pd.read_parquet(args.data / "wallets.parquet"),
        pd.read_parquet(args.data / "sim_swaps.parquet"),
        config=config,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print(f"wrote features for {len(table):,} transactions to {args.out}")
    return 0


# Scorers `txrisk evaluate` can judge. LOF is an offline comparison only; it is never served.
SCORERS = [RULES_ONLY, "anomaly", "lof", "supervised", "stacked"]


def _feature_config(args: argparse.Namespace) -> FeatureConfig:
    market = load_config(args.config).market
    return load_feature_config(
        args.feature_config,
        kyc_limits={tier.name: tier.single_transaction_limit for tier in market.kyc_tiers},
    )


def _tables(data: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Transactions, Wallets and SIM swaps."""
    return (
        pd.read_parquet(data / "transactions.parquet"),
        pd.read_parquet(data / "wallets.parquet"),
        pd.read_parquet(data / "sim_swaps.parquet"),
    )


def _evaluate(args: argparse.Namespace) -> int:
    features = _feature_config(args)
    evaluation = load_evaluation_config(args.evaluation_config)
    tables = _tables(args.data)
    period_start = load_config(args.config).period.start
    if args.scorer == RULES_ONLY:
        scores = rules_only_scores(
            *tables,
            features=features,
            rules=load_rules_config(args.rules_config),
            evaluation=evaluation,
            period_start=period_start,
        )
    elif args.scorer == "stacked":
        scores = stacked_scores(
            *tables,
            pd.read_parquet(args.data / "fraud_discoveries.parquet"),
            features=features,
            rules=load_rules_config(args.rules_config),
            evaluation=evaluation,
            period_start=period_start,
            feature_rows=pd.read_parquet(args.features) if args.features else None,
        )
    elif args.scorer == "supervised":
        scores = supervised_scores(
            *tables,
            pd.read_parquet(args.data / "fraud_discoveries.parquet"),
            features=features,
            evaluation=evaluation,
            period_start=period_start,
            feature_rows=pd.read_parquet(args.features) if args.features else None,
        )
    else:
        scores = anomaly_scores(
            *tables,
            features=features,
            evaluation=evaluation,
            period_start=period_start,
            method="isolation_forest" if args.scorer == "anomaly" else "local_outlier_factor",
            feature_rows=pd.read_parquet(args.features) if args.features else None,
        )
    out = args.out or Path("reports") / args.scorer
    metrics = evaluate(
        scores, args.data, config=evaluation, period_start=period_start, scorer=args.scorer, out=out
    )
    high = metrics["at_high"]
    print(
        f"{args.scorer}: {high['per_day']:.1f} High a day, precision {high['precision']}, "
        f"recall {high['recall']}, PR-AUC {metrics['ranking']['pr_auc']}; report in {out}"
    )
    return 0


def _train(args: argparse.Namespace) -> int:
    evaluation = load_evaluation_config(args.evaluation_config)
    transactions, wallets, sim_swaps = _tables(args.data)
    if args.features:
        feature_rows = pd.read_parquet(args.features)
    else:
        feature_rows = feature_table(transactions, wallets, sim_swaps, config=_feature_config(args))
    bundle = train_bundle(
        transactions,
        feature_rows,
        pd.read_parquet(args.data / "fraud_discoveries.parquet"),
        rules=load_rules_config(args.rules_config),
        period_start=load_config(args.config).period.start,
        config=evaluation,
        seed=args.seed,
    )
    folder = args.out / bundle.version
    bundle.save(folder)
    print(f"wrote model bundle {bundle.version} to {folder}")
    if args.no_mlflow:
        return 0
    params = {
        "seed": args.seed,
        "data": str(args.data),
        "alert_budget_per_day": evaluation.alert_budget_per_day,
        "label_maturity_days": evaluation.label_maturity_days,
        **{f"splits.{name}": [window.start, window.end] for name, window in evaluation.splits},
    }
    version = log_bundle(
        bundle,
        folder,
        tracking_uri=args.mlflow_uri,
        artifacts=args.mlflow_artifacts,
        params=params,
    )
    print(f"registered as {MODEL_NAME} version {version} in {args.mlflow_uri}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="txrisk")
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("generate", help="generate the synthetic dataset")
    gen.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--out", type=Path, default=Path("data/generated"))
    gen.set_defaults(handler=_generate)

    feat = commands.add_parser("features", help="compute the feature table for a dataset")
    feat.add_argument("--data", type=Path, default=Path("data/generated"))
    feat.add_argument(
        "--config",
        type=Path,
        default=Path("config/generator.toml"),
        help="market config holding each KYC tier's single-transaction limit",
    )
    feat.add_argument("--feature-config", type=Path, default=Path("config/features.toml"))
    feat.add_argument("--out", type=Path, default=Path("data/features/features.parquet"))
    feat.set_defaults(handler=_features)

    ev = commands.add_parser("evaluate", help="score the evaluation days and write a report")
    ev.add_argument("--data", type=Path, default=Path("data/generated"))
    ev.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    ev.add_argument("--feature-config", type=Path, default=Path("config/features.toml"))
    ev.add_argument("--rules-config", type=Path, default=Path("config/rules.toml"))
    ev.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    ev.add_argument("--scorer", choices=SCORERS, default=RULES_ONLY)
    ev.add_argument(
        "--features",
        type=Path,
        help="a feature table from `txrisk features` to reuse instead of rebuilding it",
    )
    ev.add_argument("--out", type=Path, help="report folder (default: reports/<scorer>)")
    ev.set_defaults(handler=_evaluate)

    tr = commands.add_parser(
        "train", help="train a model bundle, save it and record the run in MLflow"
    )
    tr.add_argument("--data", type=Path, default=Path("data/generated"))
    tr.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    tr.add_argument("--feature-config", type=Path, default=Path("config/features.toml"))
    tr.add_argument("--rules-config", type=Path, default=Path("config/rules.toml"))
    tr.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    tr.add_argument(
        "--features",
        type=Path,
        help="a feature table from `txrisk features` to reuse instead of rebuilding it",
    )
    tr.add_argument("--seed", type=int, default=STACKED_SEED)
    tr.add_argument(
        "--out", type=Path, default=Path("models"), help="the bundle goes in <out>/<version>/"
    )
    tr.add_argument("--mlflow-uri", default="sqlite:///mlflow.db", help="MLflow tracking URI")
    tr.add_argument(
        "--mlflow-artifacts",
        type=Path,
        default=Path("mlruns"),
        help="where a new MLflow experiment keeps run artifacts",
    )
    tr.add_argument("--no-mlflow", action="store_true", help="save the bundle only")
    tr.set_defaults(handler=_train)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
