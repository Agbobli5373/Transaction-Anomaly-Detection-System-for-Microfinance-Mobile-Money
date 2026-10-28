"""The retraining job: train a new bundle from what Postgres holds at the simulated time, and
register it as the Challenger model.

The training set is Transactions, SIM swaps and Fraud labels as they stood at the job's cutoff, the
start of the simulated day the clock is in (ADR 0005); nothing after it is read, and Ground truth
is never opened (ADR 0007). The Label maturity window is honoured as in the first Champion
model's training: a Fraud label counts from the moment it was discovered, and an undiscovered
Transaction counts as legitimate only once it is older than the window (ADR 0008).

The splits slide with the cutoff, keeping the lengths the evaluation config gives them: the
models train on every day up to the stacker's, the stacker fits on the calibration days, and the
thresholds are set on the most recent days, which need no labels. Retraining at the cutoff the
first Champion went live at reproduces that Champion's splits.

The bundle is saved under the models folder, then named in `challenger_model` in the same
transaction that records the run, so a worker that sees the new Challenger finds the bundle there.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import sqlalchemy as sa

from txrisk.evaluation import EvaluationConfig
from txrisk.evaluation.config import Days, Splits
from txrisk.features import FeatureConfig, feature_table
from txrisk.models.bundle import train_bundle
from txrisk.models.registry import log_bundle
from txrisk.monitoring.config import MonitoringConfig
from txrisk.retraining.config import RetrainingConfig
from txrisk.scoring import RulesConfig
from txrisk.store.labels import FraudLabels
from txrisk.store.tables import (
    challenger_model,
    monitoring_metrics,
    retraining_runs,
    sim_swaps,
    transactions,
    wallets,
)

log = logging.getLogger(__name__)

# The Postgres advisory lock a retraining run holds, so two runs (the weekly job and the drift
# check can start together) never train and register at once.
_LOCK = 22_000_022

WEEKLY = "weekly"
DRIFT = "drift"


class NotEnoughHistory(ValueError):
    """The simulated time is too early for the splits to fit before it."""


class AlreadyRunning(RuntimeError):
    """Another retraining run holds the lock."""


@dataclass(frozen=True)
class TrainingSet:
    """What retraining learns from, all of it visible at the cutoff."""

    transactions: pd.DataFrame
    wallets: pd.DataFrame
    sim_swaps: pd.DataFrame
    # Fraud labels as a discoveries table: transaction_id, discovered_at, source.
    discoveries: pd.DataFrame


@dataclass(frozen=True)
class Retrained:
    trigger: str
    reason: str
    cutoff: datetime
    bundle_version: str
    transactions: int
    fraud_labels: int
    folder: Path
    # The version MLflow registered it as; None if MLflow was not used.
    registry_version: str | None


def training_cutoff(clock: datetime) -> datetime:
    """The start of the simulated day the clock is in: the moment the training set is cut at."""
    moment = clock.astimezone(UTC)
    return datetime(moment.year, moment.month, moment.day, tzinfo=UTC)


def shifted_evaluation(
    config: EvaluationConfig, cutoff: datetime, period_start: date
) -> EvaluationConfig:
    """`config` with its splits moved so that the live period starts at `cutoff`: the thresholds
    and calibration days keep their lengths and sit just before it (the thresholds right before,
    the calibration days before those), the training days run from the first day to the
    calibration days, and the evaluation days keep their length after it."""
    splits = config.splits
    day = (
        cutoff - datetime(period_start.year, period_start.month, period_start.day, tzinfo=UTC)
    ).days
    thresholds_end = day
    thresholds_start = thresholds_end - (splits.thresholds.end - splits.thresholds.start)
    calibration_start = thresholds_start - (splits.calibration.end - splits.calibration.start)
    if calibration_start <= 0:
        raise NotEnoughHistory(
            f"day {day} is too early to retrain: the calibration and threshold days alone take "
            f"{thresholds_end - calibration_start} days, leaving none to train the models on"
        )
    return config.model_copy(
        update={
            "splits": Splits(
                train=Days(start=0, end=calibration_start),
                calibration=Days(start=calibration_start, end=thresholds_start),
                thresholds=Days(start=thresholds_start, end=thresholds_end),
                evaluation=Days(
                    start=day, end=day + (splits.evaluation.end - splits.evaluation.start)
                ),
            )
        }
    )


def load_training_set(engine: sa.Engine, cutoff: datetime) -> TrainingSet:
    """The Transactions and SIM swaps before `cutoff`, the Wallets registered by then, and the
    Fraud labels discovered before it, from Postgres: the only source retraining reads."""
    with engine.connect() as connection:
        found = {
            "transactions": connection.execute(
                sa.select(transactions)
                .where(transactions.c.occurred_at < cutoff)
                .order_by(transactions.c.occurred_at, transactions.c.transaction_id)
            ),
            "wallets": connection.execute(
                sa.select(wallets).where(wallets.c.registered_at < cutoff)
            ),
            "sim_swaps": connection.execute(
                sa.select(sim_swaps)
                .where(sim_swaps.c.occurred_at < cutoff)
                .order_by(sim_swaps.c.occurred_at)
            ),
        }
        frames = {
            name: pd.DataFrame(rows.all(), columns=list(rows.keys()))
            for name, rows in found.items()
        }
    for name, column in (
        ("transactions", "occurred_at"),
        ("wallets", "registered_at"),
        ("sim_swaps", "occurred_at"),
    ):
        frames[name][column] = pd.to_datetime(frames[name][column], utc=True)
    labels = FraudLabels(engine).known_at(cutoff)
    discoveries = pd.DataFrame(
        {
            "transaction_id": [label.transaction_id for label in labels],
            "discovered_at": pd.to_datetime([label.discovered_at for label in labels], utc=True),
            "source": [label.source for label in labels],
        }
    )
    return TrainingSet(frames["transactions"], frames["wallets"], frames["sim_swaps"], discoveries)


def drift_finding(
    engine: sa.Engine,
    *,
    clock: datetime,
    monitoring: MonitoringConfig,
    config: RetrainingConfig,
) -> str | None:
    """Why retraining should run now because of drift, or None if it should not.

    It should when the daily monitoring job has stored a PSI at or above the action threshold for a
    day since the last retraining run's cutoff, and that run was at least `min_days_between_runs`
    ago. Monitored days before the cutoff are not new: that run has already seen them. Read from
    the metrics monitoring stored, the same findings that fire the Alertmanager's action alerts.
    """
    with engine.connect() as connection:
        last: datetime | None = connection.execute(
            sa.select(sa.func.max(retraining_runs.c.cutoff))
        ).scalar_one()
        if last is not None and clock - last < timedelta(days=config.drift.min_days_between_runs):
            return None
        query = (
            sa.select(
                monitoring_metrics.c.day,
                monitoring_metrics.c.metric,
                monitoring_metrics.c.member,
                monitoring_metrics.c.value,
            )
            .where(
                monitoring_metrics.c.metric.in_(["psi", "psi_alerted"]),
                monitoring_metrics.c.value >= monitoring.psi.action,
            )
            .order_by(monitoring_metrics.c.day.desc(), monitoring_metrics.c.value.desc())
            .limit(1)
        )
        if last is not None:
            query = query.where(monitoring_metrics.c.day >= last.astimezone(UTC).date())
        row = connection.execute(query).first()
    if row is None:
        return None
    where = "among the Alerts" if row.metric == "psi_alerted" else "overall"
    return (
        f"PSI of {row.member} {where} was {row.value:.3f} on {row.day}, "
        "at or above the action threshold"
    )


def retrain(
    engine: sa.Engine,
    *,
    trigger: str,
    reason: str,
    clock: datetime,
    models: Path,
    rules: RulesConfig,
    features: FeatureConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    seed: int,
    baseline_bins: int,
    mlflow_uri: str | None = None,
    mlflow_artifacts: Path = Path("mlruns"),
) -> Retrained:
    """Train a bundle on the training set at `clock`, save it in `models/<version>/` and make it
    the Challenger model. Raises `NotEnoughHistory` if the time is too early, `AlreadyRunning` if
    another run holds the lock."""
    cutoff = training_cutoff(clock)
    shifted = shifted_evaluation(evaluation, cutoff, period_start)
    with engine.connect() as lock:
        if not lock.execute(sa.select(sa.func.pg_try_advisory_lock(_LOCK))).scalar_one():
            raise AlreadyRunning("another retraining run is in progress")
        try:
            return _retrain(
                engine,
                trigger=trigger,
                reason=reason,
                cutoff=cutoff,
                models=models,
                rules=rules,
                features=features,
                evaluation=shifted,
                period_start=period_start,
                seed=seed,
                baseline_bins=baseline_bins,
                mlflow_uri=mlflow_uri,
                mlflow_artifacts=mlflow_artifacts,
            )
        finally:
            lock.execute(sa.select(sa.func.pg_advisory_unlock(_LOCK)))


def _retrain(
    engine: sa.Engine,
    *,
    trigger: str,
    reason: str,
    cutoff: datetime,
    models: Path,
    rules: RulesConfig,
    features: FeatureConfig,
    evaluation: EvaluationConfig,
    period_start: date,
    seed: int,
    baseline_bins: int,
    mlflow_uri: str | None,
    mlflow_artifacts: Path,
) -> Retrained:
    data = load_training_set(engine, cutoff)
    log.info(
        "retraining at %s on %d Transactions and %d Fraud labels",
        cutoff,
        len(data.transactions),
        len(data.discoveries),
    )
    rows = feature_table(data.transactions, data.wallets, data.sim_swaps, config=features)
    bundle = train_bundle(
        data.transactions,
        rows,
        data.discoveries,
        rules=rules,
        period_start=period_start,
        config=evaluation,
        seed=seed,
        baseline_bins=baseline_bins,
        wallets=data.wallets,
    )
    folder = models / bundle.version
    bundle.save(folder)
    registry_version = None
    if mlflow_uri is not None:
        registry_version = log_bundle(
            bundle,
            folder,
            tracking_uri=mlflow_uri,
            artifacts=mlflow_artifacts,
            params={
                "seed": seed,
                "trigger": trigger,
                "cutoff": cutoff.isoformat(),
                "fraud_labels": len(data.discoveries),
                "alert_budget_per_day": evaluation.alert_budget_per_day,
                "label_maturity_days": evaluation.label_maturity_days,
                **{f"splits.{n}": [w.start, w.end] for n, w in evaluation.splits},
            },
            alias="challenger",
        )
    with engine.begin() as connection:
        run_id = connection.execute(
            sa.insert(retraining_runs)
            .values(
                trigger=trigger,
                cutoff=cutoff,
                bundle_version=bundle.version,
                registry_version=registry_version,
                transactions=len(data.transactions),
                fraud_labels=len(data.discoveries),
                reason=reason,
            )
            .returning(retraining_runs.c.run_id)
        ).scalar_one()
        connection.execute(
            sa.update(challenger_model)
            .where(challenger_model.c.singleton)
            .values(bundle_version=bundle.version, run_id=run_id)
        )
    return Retrained(
        trigger=trigger,
        reason=reason,
        cutoff=cutoff,
        bundle_version=bundle.version,
        transactions=len(data.transactions),
        fraud_labels=len(data.discoveries),
        folder=folder,
        registry_version=registry_version,
    )
