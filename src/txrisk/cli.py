"""Command-line entry point: `txrisk <command>`."""

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from txrisk.benchmark import run_paysim
from txrisk.benchmark.paysim import SCORERS as PAYSIM_SCORERS
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
from txrisk.store import create_engine, migrate, pending_migrations, seed

PSEUDONYM_KEY_ENV = "TXRISK_PSEUDONYM_KEY"
DATABASE_URL_ENV = "TXRISK_DATABASE_URL"


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


def _paysim(args: argparse.Namespace) -> int:
    scorers = PAYSIM_SCORERS if args.scorer == "all" else [args.scorer]
    results = run_paysim(
        args.csv,
        out=args.out,
        scorers=scorers,
        evaluation_config=args.evaluation_config,
        feature_config=args.feature_config,
        rules_config=args.rules_config,
        features_cache=args.features,
    )
    for scorer, metrics in results.items():
        high = metrics["at_high"]
        print(
            f"{scorer}: {high['per_day']:.1f} High a day, precision {high['precision']}, "
            f"recall {high['recall']}, PR-AUC {metrics['ranking']['pr_auc']}; "
            f"report in {args.out / scorer}"
        )
    return 0


def _database_url(args: argparse.Namespace) -> str | None:
    url = args.database_url or os.environ.get(DATABASE_URL_ENV)
    if not url:
        print(f"error: pass --database-url or set {DATABASE_URL_ENV}", file=sys.stderr)
    return url


def _migrate(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    applied = migrate(create_engine(url))
    print(f"applied {', '.join(applied)}" if applied else "schema already up to date")
    return 0


def _seed(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    until_day = args.until_day
    if until_day is None:
        until_day = load_evaluation_config(args.evaluation_config).splits.evaluation.start
    until = pd.Timestamp(load_config(args.config).period.start, tz="UTC") + pd.Timedelta(
        days=until_day
    )
    engine = create_engine(url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    counts = seed(engine, args.data, until=until.to_pydatetime())
    stored = ", ".join(f"{n:,} {table}" for table, n in counts.items())
    print(f"seeded history before day {until_day} ({until:%Y-%m-%d}): {stored}")
    return 0


def _issue_api_key(args: argparse.Namespace) -> int:
    from txrisk.api.auth import SCOPES
    from txrisk.store import api_keys

    url = _database_url(args)
    if url is None:
        return 2
    unknown = sorted(set(args.scope) - set(SCOPES))
    if unknown:
        print(
            f"error: unknown scope {', '.join(unknown)}; one of {', '.join(SCOPES)}",
            file=sys.stderr,
        )
        return 2
    # Real time, not event time: API keys expire in the real world (ADR 0005 covers scoring).
    now = datetime.now(UTC)
    with create_engine(url).begin() as connection:
        issued = api_keys.issue(
            connection,
            name=args.name,
            scopes=args.scope,
            rate_limit_per_minute=args.rate_limit,
            created_by="txrisk api-key",
            now=now,
        )
    print(
        f"issued API key {issued.record.key_id} for {args.name}; it is shown only once:",
        file=sys.stderr,
    )
    print(issued.key)
    return 0


def _demo_sign_in(args: argparse.Namespace) -> int:
    from txrisk.api.signin import demo_access, demo_signin_env

    realm_url = args.realm_url or os.environ.get("TXRISK_OIDC_ISSUER")
    if not realm_url:
        print("error: pass --realm-url or set TXRISK_OIDC_ISSUER", file=sys.stderr)
        return 2
    if not os.environ.get(demo_signin_env(args.role)):
        print(f"error: set {demo_signin_env(args.role)} (see .env.example)", file=sys.stderr)
        return 2
    print(demo_access(realm_url, args.role))
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("txrisk.api:app_from_env", factory=True, host=args.host, port=args.port)
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

    ps = commands.add_parser(
        "paysim", help="judge the scorers on a local PaySim CSV, as an external benchmark"
    )
    ps.add_argument("--csv", type=Path, required=True, help="PaySim's PS_*_log.csv, from Kaggle")
    ps.add_argument("--evaluation-config", type=Path, default=Path("config/paysim.toml"))
    ps.add_argument("--feature-config", type=Path, default=Path("config/features.toml"))
    ps.add_argument("--rules-config", type=Path, default=Path("config/rules.toml"))
    ps.add_argument("--scorer", choices=[*PAYSIM_SCORERS, "all"], default="all")
    ps.add_argument(
        "--features",
        type=Path,
        help="where to keep PaySim's feature table: read if it exists, else built and saved there",
    )
    ps.add_argument("--out", type=Path, default=Path("reports/paysim"), help="a folder per scorer")
    ps.set_defaults(handler=_paysim)

    mig = commands.add_parser("migrate", help="apply pending Postgres schema migrations")
    mig.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    mig.set_defaults(handler=_migrate)

    sd = commands.add_parser(
        "seed", help="load a dataset's parties, and its history before a day, into Postgres"
    )
    sd.add_argument("--data", type=Path, default=Path("data/generated"))
    sd.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    sd.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    sd.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    sd.add_argument(
        "--until-day",
        type=int,
        help="load SIM swaps and Transactions before this day (default: the evaluation start)",
    )
    sd.set_defaults(handler=_seed)

    key = commands.add_parser(
        "api-key", help="issue a scoped API key for a service, straight into Postgres"
    )
    key.add_argument("--name", required=True, help="the service that will hold it")
    key.add_argument(
        "--scope", action="append", required=True, help="repeat for each, e.g. score:write"
    )
    key.add_argument("--rate-limit", type=int, default=600, help="requests a minute")
    key.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    key.set_defaults(handler=_issue_api_key)

    demo = commands.add_parser(
        "demo-sign-in", help="print an access token for a demo user of the Keycloak realm"
    )
    demo.add_argument("role", choices=["analyst", "supervisor", "admin"])
    demo.add_argument("--realm-url", help="where to reach the realm (default: $TXRISK_OIDC_ISSUER)")
    demo.set_defaults(handler=_demo_sign_in)

    srv = commands.add_parser("serve", help="run the scoring API, configured by TXRISK_* variables")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    srv.set_defaults(handler=_serve)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
