"""Command-line entry point: `txrisk <command>`."""

import argparse
import json
import logging
import os
import signal
import sys
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import sqlalchemy as sa

from txrisk.api import ApiSettings
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
from txrisk.events import (
    DEFAULT_SECONDS_PER_DAY,
    EVENTS,
    OutboxPublisher,
    Pacer,
    Publisher,
    ScoringWorker,
    Topics,
    WorkerSettings,
    ensure_topics,
    json_schema,
    ledger_events,
    publish_ledger,
    replay,
    replay_events,
    schema_file,
)
from txrisk.features import FeatureConfig, feature_table, load_feature_config
from txrisk.generator import generate, load_config, write_dataset
from txrisk.loadtest import (
    check_stream,
    check_sync,
    load_loadtest_config,
    read_stream_result,
    read_sync_result,
)
from txrisk.models.bundle import train_bundle
from txrisk.models.registry import MODEL_NAME, log_bundle
from txrisk.monitoring import load_monitoring_config
from txrisk.observability.logs import configure_logging
from txrisk.observability.metrics import Metrics
from txrisk.observability.tracing import configure_tracing, shutdown_tracing
from txrisk.scheduler import (
    ComposeLauncher,
    JobLauncher,
    KubernetesLauncher,
    RestKubernetesApi,
    Scheduler,
    SchedulerConfig,
    load_scheduler_config,
)
from txrisk.scoring import RULES_ONLY, load_rules_config
from txrisk.scoring import Metrics as ScoringMetrics
from txrisk.store import (
    BackupError,
    FraudLabels,
    RedisHistoryCache,
    SimulationClock,
    StoredScoring,
    backup,
    check_restore,
    create_app_engine,
    create_engine,
    latest_backup,
    load_backup_config,
    load_retention_config,
    migrate,
    pending_migrations,
    provision_app_role,
    purge,
    restore,
    seed,
)
from txrisk.store.champion import champion_folder
from txrisk.store.roles import APP_ROLE, PASSWORD_ENV, app_role_password

PSEUDONYM_KEY_ENV = "TXRISK_PSEUDONYM_KEY"
DATABASE_URL_ENV = "TXRISK_DATABASE_URL"
KAFKA_ENV = "TXRISK_KAFKA_BOOTSTRAP_SERVERS"
MODEL_BUNDLE_ENV = "TXRISK_MODEL_BUNDLE"
PUSHGATEWAY_ENV = "TXRISK_PUSHGATEWAY_URL"
MODELS_DIR_ENV = "TXRISK_MODELS_DIR"
MLFLOW_URI_ENV = "TXRISK_MLFLOW_URI"
MLFLOW_ARTIFACTS_ENV = "TXRISK_MLFLOW_ARTIFACTS"


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
        baseline_bins=load_monitoring_config(args.monitoring_config).baseline.bins,
        wallets=wallets,
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
        "surge_elasticity": evaluation.surge_elasticity,
        "surge_max_divisor": evaluation.surge_max_divisor,
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
    engine = create_engine(url)
    applied = migrate(engine)
    print(f"applied {', '.join(applied)}" if applied else "schema already up to date")
    # The API and workers connect as a role with only the rights they need (txrisk.store.roles);
    # its password comes from the environment, never from a file in git.
    password = app_role_password()
    if password is None:
        print(
            f"warning: {PASSWORD_ENV} is not set, so the {APP_ROLE} database role was not "
            "provisioned; the API and workers cannot connect as it",
            file=sys.stderr,
        )
    else:
        provision_app_role(engine, password)
        print(f"provisioned the {APP_ROLE} database role")
    engine.dispose()
    return 0


def _day_start(args: argparse.Namespace, day: int) -> datetime:
    """The start of a day of the generated period, in UTC."""
    start = pd.Timestamp(load_config(args.config).period.start, tz="UTC")
    return (start + pd.Timedelta(days=day)).to_pydatetime()


def _evaluation_start(args: argparse.Namespace) -> int:
    """The first day of the live period, up to which `seed` loads history by default."""
    return load_evaluation_config(args.evaluation_config).splits.evaluation.start


def _backup(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    config = load_backup_config(args.config)
    keep = config.keep if args.keep is None else args.keep
    try:
        written = backup(url, args.out or config.directory, keep=keep)
    except (BackupError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"wrote {written}")
    return 0


def _restore(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    source = args.backup
    if source.is_dir():
        found = latest_backup(source)
        if found is None:
            print(f"error: no backups in {source}", file=sys.stderr)
            return 1
        source = found
    try:
        restore(source, url)
    except BackupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"restored {source}")
    if args.compare_with:
        check = check_restore(create_engine(args.compare_with), create_engine(url))
        print(f"latest migration: {check.restored_migration}; rows: {check.restored_counts}")
        if not check.ok:
            print(
                f"error: the restore differs from the source: {check.source_counts}",
                file=sys.stderr,
            )
            return 1
    return 0


def _seed(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    until_day = args.until_day
    if until_day is None:
        until_day = _evaluation_start(args)
    until = _day_start(args, until_day)
    engine = create_engine(url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    counts = seed(engine, args.data, until=until)
    stored = ", ".join(f"{n:,} {table}" for table, n in counts.items())
    print(f"seeded history before day {until_day} ({until:%Y-%m-%d}): {stored}")
    # Seeding stores Transactions behind the history cache's back, so whatever it holds is stale.
    redis_url = os.environ.get("TXRISK_REDIS_URL")
    if redis_url and counts["transactions"]:
        RedisHistoryCache.from_url(redis_url).flush()
        print("flushed the recent-history cache")
    return 0


def _seed_cases(args: argparse.Namespace) -> int:
    from txrisk.store.demo_cases import seed_demo_cases

    url = _database_url(args)
    if url is None:
        return 2
    if bool(args.assignee_id) != bool(args.assignee_name):
        print("error: pass --assignee-id and --assignee-name together", file=sys.stderr)
        return 2
    engine = create_engine(url)
    with engine.begin() as connection:
        pending = pending_migrations(connection)
        if pending:
            print(
                f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr
            )
            return 1
        made = seed_demo_cases(
            connection,
            tag=args.tag,
            count=args.count,
            medium=args.medium,
            assignee=(args.assignee_id, args.assignee_name) if args.assignee_id else None,
        )
    print(json.dumps({"cases": [{"case_id": c.case_id, "wallet_id": c.wallet_id} for c in made]}))
    return 0


def _issue_api_key(args: argparse.Namespace) -> int:
    from txrisk.api.auth import SCOPES
    from txrisk.store import api_keys, audit
    from txrisk.store.audit import Actor, AuditEntry

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
    # Run inside the API container, this connects as the application role.
    with create_app_engine(url).begin() as connection:
        issued = api_keys.issue(
            connection,
            name=args.name,
            scopes=args.scope,
            rate_limit_per_minute=args.rate_limit,
            created_by="txrisk api-key",
            now=now,
        )
        # An operator with database access issues keys too, so it is on the audit log as well.
        audit.record(
            connection,
            AuditEntry(
                occurred_at=now,
                actor=Actor(type="operator", id="txrisk api-key", name="txrisk api-key"),
                action="api_key.issue",
                object_type="api_key",
                object_id=issued.record.key_id,
                detail={
                    "name": args.name,
                    "scopes": sorted(args.scope),
                    "rate_limit_per_minute": args.rate_limit,
                },
            ),
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
        print(
            f"error: set {demo_signin_env(args.role)} (see keycloak.env.example)", file=sys.stderr
        )
        return 2
    print(demo_access(realm_url, args.role))
    return 0


def _observe(service: str, metrics_port: int | None = None) -> Metrics:
    """Structured JSON logs, tracing if `TXRISK_TRACING_ENABLED` is set, and Prometheus metrics
    served on `metrics_port` (default `$TXRISK_METRICS_PORT`, else not served)."""
    configure_logging()
    configure_tracing(service)
    metrics = Metrics()
    port = metrics_port or int(os.environ.get("TXRISK_METRICS_PORT") or 0)
    if port:
        metrics.serve(port)
    return metrics


def _produce(args: argparse.Namespace) -> int:
    servers = args.bootstrap_servers or os.environ.get(KAFKA_ENV)
    if not servers:
        print(f"error: pass --bootstrap-servers or set {KAFKA_ENV}", file=sys.stderr)
        return 2
    metrics = _observe("txrisk-produce", args.metrics_port)
    from_day = _evaluation_start(args) if args.from_day is None else args.from_day
    since = _day_start(args, from_day)
    until = None if args.until_day is None else _day_start(args, args.until_day)
    topics = Topics(args.topic_prefix)
    ensure_topics(servers, topics, partitions=args.partitions)
    published = publish_ledger(
        Publisher(servers, metrics=metrics),
        topics,
        ledger_events(args.data, since=since, until=until),
        limit=args.limit,
    )
    shutdown_tracing()
    print(
        f"published {published.transactions:,} Transactions to {topics.transactions} and "
        f"{published.sim_swaps:,} SIM swaps to {topics.sim_swaps}, from day {from_day}"
    )
    return 0


def _replay(args: argparse.Namespace) -> int:
    servers = args.bootstrap_servers or os.environ.get(KAFKA_ENV)
    if not servers:
        print(f"error: pass --bootstrap-servers or set {KAFKA_ENV}", file=sys.stderr)
        return 2
    if args.seconds_per_day < 0:
        print("error: --seconds-per-day cannot be negative", file=sys.stderr)
        return 2
    metrics = _observe("txrisk-replay", args.metrics_port)
    from_day = _evaluation_start(args) if args.from_day is None else args.from_day
    since = _day_start(args, from_day)
    until = None if args.until_day is None else _day_start(args, args.until_day)
    topics = Topics(args.topic_prefix)
    ensure_topics(servers, topics, partitions=args.partitions)
    replayed = replay(
        Publisher(servers, metrics=metrics),
        topics,
        replay_events(args.data, since=since, until=until),
        Pacer(args.seconds_per_day),
        limit=args.limit,
    )
    shutdown_tracing()
    print(
        f"replayed {replayed.transactions:,} Transactions, {replayed.sim_swaps:,} SIM swaps and "
        f"{replayed.fraud_discoveries:,} Fraud discoveries from day {from_day}, "
        f"at {args.seconds_per_day:g} s per simulated day"
    )
    return 0


def _loadtest_check(args: argparse.Namespace) -> int:
    if args.sync is None and args.stream is None:
        print("error: pass --sync and/or --stream", file=sys.stderr)
        return 2
    config = load_loadtest_config(args.config)
    checks = []
    try:
        if args.sync is not None:
            checks += [("sync", c) for c in check_sync(read_sync_result(args.sync), config.sync)]
        if args.stream is not None:
            checks += [
                ("stream", c) for c in check_stream(read_stream_result(args.stream), config.stream)
            ]
    except (OSError, ValueError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for part, check in checks:
        print(f"{'PASS' if check.passed else 'FAIL'}  {part} {check.name}: {check.detail}")
    return 0 if all(c.passed for _, c in checks) else 1


def _purge(args: argparse.Namespace) -> int:
    url = _database_url(args)
    if url is None:
        return 2
    if sa.make_url(url).username == APP_ROLE:
        print(
            f"error: purge deletes data, which {APP_ROLE} may not; run it as the owner "
            "(compose runs it in the job service)",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(url)
    purged = purge(engine, SimulationClock(engine), load_retention_config(args.config))
    if purged is None:
        print("nothing purged: the simulation clock has not started")
        return 0
    deleted = ", ".join(f"{n:,} {table}" for table, n in purged.deleted.items())
    print(f"purged at {purged.at:%Y-%m-%d %H:%M}: {deleted}")
    return 0


def _launcher(config: SchedulerConfig, kind: str) -> JobLauncher:
    if kind == "kubernetes":
        return KubernetesLauncher(config.kubernetes, RestKubernetesApi.in_cluster(os.environ))
    return ComposeLauncher(config.compose)


def _scheduler(args: argparse.Namespace) -> int:
    metrics = _observe("txrisk-scheduler", args.metrics_port)
    url = _database_url(args)
    if url is None:
        return 2
    config = load_scheduler_config(args.config)
    # The scheduler only reads the clock and records what it fired, so it runs as the application
    # role; the jobs it launches run in the `job` container, as the owner.
    engine = create_app_engine(url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    scheduler = Scheduler(
        engine,
        SimulationClock(engine),
        _launcher(config, args.launcher),
        config.jobs,
        timezone=ZoneInfo(config.timezone),
        metrics=metrics,
    )
    if args.once:
        for fired in scheduler.tick():
            print(f"fired {fired.job} for {fired.period} as {fired.launch_ref}")
        return 0
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda received, _: stop.set())
    logging.getLogger(__name__).info("scheduling %s", ", ".join(job.name for job in config.jobs))
    # `stop.wait` is the wait between ticks, so a signal ends it at once.
    scheduler.run(stop.is_set, stop.wait, poll_seconds=args.poll_seconds)
    engine.dispose()
    return 0


def _monitor(args: argparse.Namespace) -> int:
    from txrisk.models.bundle import ModelBundle
    from txrisk.monitoring import job
    from txrisk.monitoring import metrics as exposition

    configure_logging()
    url = _database_url(args)
    if url is None:
        return 2
    if sa.make_url(url).username == APP_ROLE:
        print(
            f"error: monitoring stores its metrics, which {APP_ROLE} may not; run it as the owner "
            "(compose runs it in the job service)",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    clock = SimulationClock(engine).peek()
    if args.day is None and clock is None:
        print("nothing monitored: the simulation clock has not started")
        return 0
    # The scheduler fires the job as the clock enters a day, so by default the day just ended.
    day = args.day or job.latest_complete_day(clock)  # type: ignore[arg-type]
    bundle_folder = args.bundle or (
        Path(os.environ[MODEL_BUNDLE_ENV]) if os.environ.get(MODEL_BUNDLE_ENV) else None
    )
    bundle = None
    # After a Promotion or rollback the Champion model is the one Postgres names, saved with the
    # other bundles; until one the configured bundle is.
    models_dir = (
        Path(os.environ[MODELS_DIR_ENV])
        if os.environ.get(MODELS_DIR_ENV)
        else (None if bundle_folder is None else bundle_folder.parent)
    )
    bundle_folder = champion_folder(engine, bundle_folder, models_dir)
    if bundle_folder is not None:
        try:
            bundle = ModelBundle.load(bundle_folder)
        except Exception as error:  # a bundle that will not load is reported, not fatal
            print(f"warning: could not load the Champion model bundle: {error}", file=sys.stderr)
    monitored = job.monitor_day(
        engine,
        model_version=None if bundle is None else bundle.version,
        baseline=None if bundle is None else bundle.baseline,
        config=load_monitoring_config(args.config),
        evaluation=load_evaluation_config(args.evaluation_config),
        day=day,
        # Labels are read as of the clock; for a day named explicitly, as of that day's end.
        as_of=clock if args.day is None else None,
    )
    print(
        f"monitored {monitored.day} for "
        f"{monitored.model_version or 'no Champion model'}: "
        f"{len(monitored.metrics)} metrics stored"
    )
    if monitored.baseline_missing:
        print("no baseline: the bundle carries none, so drift was not measured")
    for name in monitored.psi_warnings:
        print(f"warning: PSI of {name} is at or above the warning threshold")
    for name in monitored.psi_actions:
        print(f"action: PSI of {name} is at or above the action threshold")
    gateway = args.pushgateway or os.environ.get(PUSHGATEWAY_ENV)
    if gateway:
        try:
            exposition.push(gateway, monitored.metrics)
        except OSError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print(f"pushed to {gateway}")
    engine.dispose()
    return 0


def _retrain(args: argparse.Namespace) -> int:
    from txrisk.retraining import (
        AlreadyRunning,
        NotEnoughHistory,
        drift_finding,
        load_retraining_config,
        retrain,
    )
    from txrisk.retraining.job import DRIFT, WEEKLY

    configure_logging()
    url = _database_url(args)
    if url is None:
        return 2
    if sa.make_url(url).username == APP_ROLE:
        print(
            f"error: retraining registers a model, which {APP_ROLE} may not; run it as the owner "
            "(compose runs it in the job service)",
            file=sys.stderr,
        )
        return 2
    engine = create_engine(url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    clock = SimulationClock(engine).peek()
    if clock is None:
        print("nothing retrained: the simulation clock has not started")
        return 0
    trigger, reason = WEEKLY, "weekly schedule"
    if args.if_drift:
        found = drift_finding(
            engine,
            clock=clock,
            monitoring=load_monitoring_config(args.monitoring_config),
            config=load_retraining_config(args.retraining_config),
        )
        if found is None:
            print("nothing retrained: no drift action alert since the last retraining run")
            return 0
        trigger, reason = DRIFT, found
    mlflow_uri = None if args.no_mlflow else args.mlflow_uri
    try:
        done = retrain(
            engine,
            trigger=trigger,
            reason=reason,
            clock=clock,
            models=args.models,
            rules=load_rules_config(args.rules_config),
            features=_feature_config(args),
            evaluation=load_evaluation_config(args.evaluation_config),
            period_start=load_config(args.config).period.start,
            seed=args.seed,
            baseline_bins=load_monitoring_config(args.monitoring_config).baseline.bins,
            mlflow_uri=mlflow_uri,
            mlflow_artifacts=args.mlflow_artifacts,
        )
    except NotEnoughHistory as error:
        print(f"nothing retrained: {error}")
        return 0
    except AlreadyRunning as error:
        print(f"nothing retrained: {error}")
        return 0
    engine.dispose()
    print(
        f"retrained at {done.cutoff:%Y-%m-%d} ({done.reason}) on {done.transactions:,} "
        f"Transactions and {done.fraud_labels:,} Fraud labels: Challenger model "
        f"{done.bundle_version} in {done.folder}"
        + (f", registered as {MODEL_NAME} version {done.registry_version}" if mlflow_uri else "")
    )
    return 0


def _alert_sink(args: argparse.Namespace) -> int:
    from txrisk.monitoring import sink

    configure_logging()
    server = sink.serve(sink.AlertSink(), args.host, args.port)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda received, _: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger(__name__).info("alert sink listening on %s:%s", args.host, args.port)
    stop.wait()
    server.shutdown()
    return 0


def _worker(args: argparse.Namespace) -> int:
    settings = WorkerSettings.from_env()
    metrics = _observe("txrisk-worker", settings.metrics_port)
    engine = create_app_engine(settings.database_url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    scoring = _stored_scoring(settings, engine, metrics)
    topics = settings.topics()
    ensure_topics(settings.kafka_bootstrap_servers, topics, partitions=settings.topic_partitions)
    worker = ScoringWorker(
        scoring,
        Publisher(settings.kafka_bootstrap_servers, metrics=metrics),
        bootstrap_servers=settings.kafka_bootstrap_servers,
        topics=topics,
        group_id=settings.consumer_group,
        batch_size=settings.batch_size,
        clock=SimulationClock(engine),
        labels=FraudLabels(engine),
        metrics=metrics,
    )
    stopping: list[int] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda received, _: stopping.append(received))
    logging.getLogger(__name__).info(
        "scoring %s with %s, history from %s",
        topics.transactions,
        "the Champion model" if settings.model_bundle else RULES_ONLY,
        "Redis, then Postgres" if settings.redis_url else "Postgres",
    )
    worker.run(lambda: bool(stopping))
    engine.dispose()
    shutdown_tracing()
    return 0


def _stored_scoring(
    settings: ApiSettings, engine: sa.Engine, metrics: ScoringMetrics
) -> StoredScoring:
    """Scoring as the API and the worker run it; a bundle that will not load means Degraded mode."""
    scoring = StoredScoring(
        engine,
        features=settings.features(),
        rules=settings.rules(),
        bundle=settings.champion(metrics, engine),
        cache=settings.history_cache(),
        metrics=metrics,
        challenger=settings.challenger(engine),
    )
    health = scoring.model_health()  # loads the bundle now, so a bad one is logged at startup
    if health.degraded:
        logging.getLogger(__name__).warning("starting in Degraded mode: %s", health.reason)
    return scoring


def _rescore(args: argparse.Namespace) -> int:
    metrics = _observe("txrisk-rescore")
    settings = ApiSettings.from_env()
    engine = create_app_engine(settings.database_url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    result = _stored_scoring(settings, engine, metrics).rescore_degraded(limit=args.limit)
    engine.dispose()
    print(
        f"re-scored {result.rescored} Transactions, raising {result.alerts_raised} Alerts; "
        f"{result.remaining} still degraded"
        + (f" (stopped: {result.stopped})" if result.stopped else "")
    )
    return 3 if result.stopped else 0


def _outbox(args: argparse.Namespace) -> int:
    settings = WorkerSettings.from_env()
    metrics = _observe("txrisk-outbox", settings.metrics_port)
    engine = create_app_engine(settings.database_url)
    with engine.connect() as connection:
        pending = pending_migrations(connection)
    if pending:
        print(f"error: run `txrisk migrate` first; pending: {', '.join(pending)}", file=sys.stderr)
        return 1
    topics = settings.topics()
    ensure_topics(settings.kafka_bootstrap_servers, topics, partitions=settings.topic_partitions)
    outbox = OutboxPublisher(
        engine, Publisher(settings.kafka_bootstrap_servers, metrics=metrics), topics=topics
    )
    if args.once:
        print(f"published {outbox.publish_pending()} events")
        return 0
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda received, _: stop.set())
    logging.getLogger(__name__).info(
        "publishing the outbox to %s", settings.kafka_bootstrap_servers
    )
    outbox.run(stop.is_set, stop.wait, poll_seconds=args.poll_seconds)
    engine.dispose()
    shutdown_tracing()
    return 0


def _schemas(args: argparse.Namespace) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    for topic in EVENTS:
        path = args.out / schema_file(topic)
        path.write_text(json.dumps(json_schema(topic), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    configure_logging()
    configure_tracing("txrisk-api")
    # The API logs each request itself (txrisk.api), as JSON with the correlation id.
    uvicorn.run(
        "txrisk.api:app_from_env",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,
        access_log=False,
    )
    shutdown_tracing()
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
    tr.add_argument("--monitoring-config", type=Path, default=Path("config/monitoring.toml"))
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

    bk = commands.add_parser(
        "backup", help="dump Postgres to a timestamped file and prune old backups"
    )
    bk.add_argument(
        "--database-url", help=f"default: ${DATABASE_URL_ENV}; the password: $PGPASSWORD"
    )
    bk.add_argument("--config", type=Path, default=Path("config/backup.toml"))
    bk.add_argument("--out", type=Path, help="the backup folder (default: from the config)")
    bk.add_argument("--keep", type=int, help="backups to keep (default: from the config)")
    bk.set_defaults(handler=_backup)

    rs = commands.add_parser("restore", help="load a backup into an empty Postgres database")
    rs.add_argument("backup", type=Path, help="a backup file, or a folder to restore the latest of")
    rs.add_argument("--database-url", help=f"the empty target; default: ${DATABASE_URL_ENV}")
    rs.add_argument(
        "--compare-with",
        metavar="URL",
        help="a database to compare the restore with: row counts per table, latest migration",
    )
    rs.set_defaults(handler=_restore)

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

    sc = commands.add_parser(
        "seed-cases",
        help="create demo Cases and Medium Transactions through the store (for the workbench)",
    )
    sc.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    sc.add_argument("--tag", default="demo", help="names the Wallets and Transactions it makes")
    sc.add_argument("--count", type=int, default=3, help="how many Cases (one per Wallet)")
    sc.add_argument("--medium", type=int, default=0, help="how many Medium-band Transactions")
    sc.add_argument("--assignee-id", help="assign every Case to this Analyst (a subject ID)")
    sc.add_argument("--assignee-name", help="the assigned Analyst's name")
    sc.set_defaults(handler=_seed_cases)

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

    prod = commands.add_parser(
        "produce",
        help="publish a dataset's Transactions and SIM swaps to the stream, in event-time order",
    )
    prod.add_argument("--data", type=Path, default=Path("data/generated"))
    prod.add_argument("--bootstrap-servers", help=f"default: ${KAFKA_ENV}")
    prod.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    prod.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    prod.add_argument(
        "--from-day",
        type=int,
        help="publish from this day on (default: the evaluation start, where `seed` stops)",
    )
    prod.add_argument("--until-day", type=int, help="publish only before this day")
    prod.add_argument("--limit", type=int, help="stop after this many Transactions")
    prod.add_argument("--topic-prefix", default="")
    prod.add_argument(
        "--partitions", type=int, default=3, help="partitions for topics it has to create"
    )
    prod.add_argument(
        "--metrics-port",
        type=int,
        help="serve Prometheus metrics here (default: $TXRISK_METRICS_PORT)",
    )
    prod.set_defaults(handler=_produce)

    rp = commands.add_parser(
        "replay",
        help=(
            "replay the live days (Transactions, SIM swaps, Fraud discoveries) through the "
            "stream at a set pace, in event-time order"
        ),
    )
    rp.add_argument("--data", type=Path, default=Path("data/generated"))
    rp.add_argument("--bootstrap-servers", help=f"default: ${KAFKA_ENV}")
    rp.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    rp.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    rp.add_argument(
        "--seconds-per-day",
        type=float,
        default=DEFAULT_SECONDS_PER_DAY,
        help="wall-clock seconds per simulated day; 0 publishes as fast as possible "
        "(default: %(default)g, a day a minute). Event times are never changed.",
    )
    rp.add_argument(
        "--from-day",
        type=int,
        help="replay from this day on (default: the evaluation start, where `seed` stops)",
    )
    rp.add_argument("--until-day", type=int, help="replay only before this day")
    rp.add_argument("--limit", type=int, help="stop after this many Transactions")
    rp.add_argument("--topic-prefix", default="")
    rp.add_argument(
        "--partitions", type=int, default=3, help="partitions for topics it has to create"
    )
    rp.add_argument(
        "--metrics-port",
        type=int,
        help="serve Prometheus metrics here (default: $TXRISK_METRICS_PORT)",
    )
    rp.set_defaults(handler=_replay)

    lt = commands.add_parser(
        "loadtest", help="judge load-test results against the targets in config/loadtest.toml"
    )
    lt_commands = lt.add_subparsers(dest="loadtest_command", required=True)
    lt_check = lt_commands.add_parser(
        "check",
        help="exit 1 unless a Locust sync run (its _stats.csv) and/or a stream run (its JSON) "
        "meet the targets",
    )
    lt_check.add_argument("--sync", type=Path, help="a Locust --csv run's <prefix>_stats.csv")
    lt_check.add_argument("--stream", type=Path, help="the JSON a stream_locustfile.py run wrote")
    lt_check.add_argument("--config", type=Path, default=Path("config/loadtest.toml"))
    lt_check.set_defaults(handler=_loadtest_check)

    pg = commands.add_parser(
        "purge", help="delete data older than its retention period, by simulation clock"
    )
    pg.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    pg.add_argument("--config", type=Path, default=Path("config/retention.toml"))
    pg.set_defaults(handler=_purge)

    sc = commands.add_parser(
        "scheduler",
        help="fire the scheduled jobs as the simulation clock crosses their schedule",
    )
    sc.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    sc.add_argument("--config", type=Path, default=Path("config/scheduler.toml"))
    sc.add_argument(
        "--launcher",
        choices=["compose", "kubernetes"],
        default="compose",
        help="how a job runs: a one-off compose container, or a Kubernetes Job (in a cluster)",
    )
    sc.add_argument(
        "--poll-seconds", type=float, default=2.0, help="wall-clock seconds between clock checks"
    )
    sc.add_argument("--once", action="store_true", help="check the clock once and exit")
    sc.add_argument(
        "--metrics-port",
        type=int,
        help="serve Prometheus metrics here (default: $TXRISK_METRICS_PORT)",
    )
    sc.set_defaults(handler=_scheduler)

    mo = commands.add_parser(
        "monitor",
        help="compute the model monitoring metrics of a simulated day: drift against the training "
        "baseline, Alert volume, Risk band mix, label-based performance, Fraud discovery sources",
    )
    mo.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    mo.add_argument("--config", type=Path, default=Path("config/monitoring.toml"))
    mo.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    mo.add_argument(
        "--bundle", type=Path, help=f"the Champion model's folder (default: ${MODEL_BUNDLE_ENV})"
    )
    mo.add_argument(
        "--day",
        type=date.fromisoformat,
        help="the simulated day to monitor, YYYY-MM-DD (default: the day before the clock's)",
    )
    mo.add_argument(
        "--pushgateway",
        help=f"push the metrics to this Pushgateway for Prometheus (default: ${PUSHGATEWAY_ENV})",
    )
    mo.set_defaults(handler=_monitor)

    rt = commands.add_parser(
        "retrain",
        help="train a new bundle from Postgres as of the simulation clock and register it as "
        "the Challenger model, which workers then score in shadow",
    )
    rt.add_argument("--database-url", help=f"default: ${DATABASE_URL_ENV}")
    rt.add_argument(
        "--if-drift",
        action="store_true",
        help="retrain only if monitoring found a drift action alert since the last run "
        "(otherwise it always retrains: the weekly job)",
    )
    rt.add_argument("--config", type=Path, default=Path("config/generator.toml"))
    rt.add_argument("--feature-config", type=Path, default=Path("config/features.toml"))
    rt.add_argument("--rules-config", type=Path, default=Path("config/rules.toml"))
    rt.add_argument("--evaluation-config", type=Path, default=Path("config/evaluation.toml"))
    rt.add_argument("--monitoring-config", type=Path, default=Path("config/monitoring.toml"))
    rt.add_argument("--retraining-config", type=Path, default=Path("config/retraining.toml"))
    rt.add_argument("--seed", type=int, default=STACKED_SEED)
    rt.add_argument(
        "--models",
        type=Path,
        default=Path(os.environ.get(MODELS_DIR_ENV) or "models"),
        help=f"where bundles live, as <models>/<version>/ (default: ${MODELS_DIR_ENV} or models)",
    )
    rt.add_argument(
        "--mlflow-uri",
        default=os.environ.get(MLFLOW_URI_ENV) or "sqlite:///mlflow.db",
        help=f"MLflow tracking URI (default: ${MLFLOW_URI_ENV} or a local file)",
    )
    rt.add_argument(
        "--mlflow-artifacts",
        type=Path,
        default=Path(os.environ.get(MLFLOW_ARTIFACTS_ENV) or "mlruns"),
        help=f"where a new MLflow experiment keeps artifacts (default: ${MLFLOW_ARTIFACTS_ENV})",
    )
    rt.add_argument("--no-mlflow", action="store_true", help="save the bundle only")
    rt.set_defaults(handler=_retrain)

    snk = commands.add_parser(
        "alert-sink", help="run the webhook that records the alerts Alertmanager sends"
    )
    snk.add_argument("--host", default="0.0.0.0")
    snk.add_argument("--port", type=int, default=9095)
    snk.set_defaults(handler=_alert_sink)

    wk = commands.add_parser(
        "worker",
        help="score transaction.created into score.produced, configured by TXRISK_* variables",
    )
    wk.set_defaults(handler=_worker)

    rsc = commands.add_parser(
        "rescore",
        help="re-score Transactions scored in Degraded mode with the Champion model, "
        "configured by TXRISK_* variables; exits 3 if the model is not healthy",
    )
    rsc.add_argument("--limit", type=int, help="re-score at most this many (default: all)")
    rsc.set_defaults(handler=_rescore)

    ob = commands.add_parser(
        "outbox",
        help="publish the transactional outbox (Fraud discoveries from closed Cases) to Redpanda",
    )
    ob.add_argument(
        "--poll-seconds", type=float, default=1.0, help="wall-clock seconds between empty checks"
    )
    ob.add_argument("--once", action="store_true", help="publish one batch and exit")
    ob.set_defaults(handler=_outbox)

    sch = commands.add_parser("schemas", help="write the event topics' JSON Schemas")
    sch.add_argument("--out", type=Path, default=Path("schemas"))
    sch.set_defaults(handler=_schemas)

    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
