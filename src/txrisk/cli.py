"""Command-line entry point: `txrisk <command>`."""

import argparse
import os
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="txrisk")
    commands = parser.add_subparsers(dest="command", required=True)

    gen = commands.add_parser("generate", help="generate the synthetic dataset")
    gen.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--out", type=Path, default=Path("data/generated"))
    gen.set_defaults(handler=_generate)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
