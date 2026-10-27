"""Command-line entry point: `txrisk <command>`."""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from txrisk.evaluation import load_evaluation_config
from txrisk.evaluation.report import evaluate
from txrisk.features import feature_table, load_feature_config
from txrisk.generator import generate, load_config, write_dataset
from txrisk.scoring import RULES_ONLY, load_rules_config, score_table

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


def _evaluate(args: argparse.Namespace) -> int:
    generator = load_config(args.config)
    features = load_feature_config(
        args.feature_config,
        kyc_limits={
            tier.name: tier.single_transaction_limit for tier in generator.market.kyc_tiers
        },
    )
    evaluation = load_evaluation_config(args.evaluation_config)
    period_start = pd.Timestamp(generator.period.start, tz="UTC")
    scores = score_table(
        pd.read_parquet(args.data / "transactions.parquet"),
        pd.read_parquet(args.data / "wallets.parquet"),
        pd.read_parquet(args.data / "sim_swaps.parquet"),
        features=features,
        rules=load_rules_config(args.rules_config),
        score_from=period_start + pd.Timedelta(days=evaluation.splits.evaluation.start),
    )
    metrics = evaluate(
        scores,
        args.data,
        config=evaluation,
        period_start=generator.period.start,
        scorer=RULES_ONLY,
        out=args.out,
    )
    high = metrics["at_high"]
    print(
        f"{RULES_ONLY}: {high['per_day']:.1f} High a day, precision {high['precision']}, "
        f"recall {high['recall']}; report in {args.out}"
    )
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
    ev.add_argument("--out", type=Path, default=Path("reports/rules-only"))
    ev.set_defaults(handler=_evaluate)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
