"""Command-line entry point: `txrisk <command>`."""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from txrisk.features import feature_table
from txrisk.generator import generate, load_config, write_dataset

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
    table = feature_table(
        pd.read_parquet(args.data / "transactions.parquet"),
        pd.read_parquet(args.data / "wallets.parquet"),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_parquet(args.out, index=False)
    print(f"wrote features for {len(table):,} transactions to {args.out}")
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
    feat.add_argument("--out", type=Path, default=Path("data/features/subject_wallet.parquet"))
    feat.set_defaults(handler=_features)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
