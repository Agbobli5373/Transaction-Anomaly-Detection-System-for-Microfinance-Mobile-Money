"""The daily monitoring job: what the Champion model and the Alert queue looked like on one
simulated day.

It reads only what the system itself stored: the model inputs and Risk assessments the scoring
path wrote, the Alerts, and the Fraud labels. Labels are read as of the simulated time the job
runs at (a discovery is invisible before its event time), and Ground truth is never read
(ADR 0007). Transactions count for label-based performance only once older than the Label maturity
window: until then an unlabelled Transaction is not known to be legitimate.

A day's rows replace any an earlier run of that day stored, so running a day again is safe.
"""

import itertools
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.evaluation import EvaluationConfig
from txrisk.monitoring.config import MonitoringConfig
from txrisk.monitoring.metrics import ALL, Metric
from txrisk.monitoring.psi import Baseline, psi
from txrisk.store.tables import (
    alerts,
    fraud_labels,
    model_inputs,
    monitoring_metrics,
    risk_assessments,
    transactions,
    wallets,
)

SEGMENTS = ("region", "kyc_tier", "channel", "wallet_age_band")
BANDS = ("Low", "Medium", "High")
SOURCES = ("complaint", "chargeback", "case")


@dataclass(frozen=True)
class Monitored:
    day: date
    # The simulated time labels were read as of.
    as_of: datetime
    model_version: str | None
    metrics: list[Metric]
    # PSI findings: the inputs (and "risk_score") at or above each threshold.
    psi_warnings: list[str]
    psi_actions: list[str]
    baseline_missing: bool


def latest_complete_day(clock: datetime) -> date:
    """The simulated day before the clock's: the latest that is complete, which is the one the
    scheduler's daily job (fired as the clock enters a new day) monitors."""
    return clock.astimezone(UTC).date() - timedelta(days=1)


def _bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def wallet_age_bands(edges: list[int]) -> list[tuple[float, str]]:
    """(upper bound in days, label) for each wallet age band; the labels are the evaluation's."""
    from txrisk.evaluation.report import _wallet_age_band

    probes = pd.Series([edges[0] - 1.0, *[float(e) for e in edges]])
    labels = list(_wallet_age_band(probes, edges))
    return list(zip([*map(float, edges), float("inf")], labels, strict=True))


def monitor_day(
    engine: sa.Engine,
    *,
    model_version: str | None,
    baseline: Baseline | None,
    config: MonitoringConfig,
    evaluation: EvaluationConfig,
    day: date,
    as_of: datetime | None = None,
) -> Monitored:
    """Compute the metrics of `day`, store them (replacing the day's earlier run) and return
    them. `model_version` and `baseline` are the Champion model's, None without one (or, for the
    baseline, with an old bundle that has none: drift is then reported as 'no baseline', not
    measured). `as_of` is the simulated time of the run, by default the day's end."""
    start, end = _bounds(day)
    as_of = end if as_of is None else as_of
    metrics: list[Metric] = [Metric("day", start.timestamp())]
    if model_version is not None:
        metrics.append(Metric("info", 1.0, member=model_version))

    drift = _drift(engine, model_version, baseline, config, start, end)
    metrics += drift.metrics
    metrics += _alert_volume(engine, config, evaluation, start, end)
    metrics += _band_mix(engine, start, end)
    metrics += _label_performance(engine, config, evaluation, day, as_of)
    metrics += _discoveries(engine, config, start, end, as_of)
    _store(engine, day, metrics)
    return Monitored(
        day=day,
        as_of=as_of,
        model_version=model_version,
        metrics=metrics,
        psi_warnings=drift.warnings,
        psi_actions=drift.actions,
        baseline_missing=drift.baseline_missing,
    )


@dataclass(frozen=True)
class _Drift:
    metrics: list[Metric]
    warnings: list[str]
    actions: list[str]
    baseline_missing: bool


def _drift(
    engine: sa.Engine,
    model_version: str | None,
    baseline: Baseline | None,
    config: MonitoringConfig,
    start: datetime,
    end: datetime,
) -> _Drift:
    thresholds = [
        Metric("psi_warn_threshold", config.psi.warn),
        Metric("psi_action_threshold", config.psi.action),
    ]
    if model_version is None or baseline is None:
        # An old bundle, or none: say so rather than fail. Nothing is measured.
        return _Drift([Metric("baseline_available", 0.0), *thresholds], [], [], True)
    ignored = set(config.psi.ignore)
    scored = (
        model_inputs.c.model_version == model_version,
        ~risk_assessments.c.degraded,
    )
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(risk_assessments.c.risk_score, model_inputs.c.inputs)
            .join(model_inputs, model_inputs.c.transaction_id == risk_assessments.c.transaction_id)
            .where(model_inputs.c.assessed_at >= start, model_inputs.c.assessed_at < end, *scored)
        ).all()
        # The Alerts of the last few days: High Transactions, whose inputs are where a change in
        # how fraud looks shows.
        alerted_rows = connection.execute(
            sa.select(model_inputs.c.inputs)
            .join(
                risk_assessments, model_inputs.c.transaction_id == risk_assessments.c.transaction_id
            )
            .where(
                model_inputs.c.assessed_at >= end - timedelta(days=config.psi.alerted_window_days),
                model_inputs.c.assessed_at < end,
                risk_assessments.c.risk_band == "High",
                *scored,
            )
        ).all()
    metrics = [
        Metric("baseline_available", 1.0),
        Metric("scored_rows", float(len(rows))),
        Metric("alerted_rows", float(len(alerted_rows))),
        *thresholds,
    ]
    found: dict[str, float] = {}
    if len(rows) >= config.psi.min_rows:
        live = pd.DataFrame([r.inputs for r in rows]).reindex(columns=list(baseline.inputs))
        found = {
            name: psi(dist, live[name])
            for name, dist in baseline.inputs.items()
            if name not in ignored
        }
        found["risk_score"] = psi(baseline.risk_score, pd.Series([r.risk_score for r in rows]))
        metrics += [Metric("psi", value, member=name) for name, value in found.items()]
    if baseline.alerted and len(alerted_rows) >= config.psi.alerted_min_rows:
        live = pd.DataFrame([r.inputs for r in alerted_rows]).reindex(
            columns=list(baseline.alerted)
        )
        among = {
            name: psi(dist, live[name])
            for name, dist in baseline.alerted.items()
            if name not in ignored
        }
        metrics += [Metric("psi_alerted", value, member=name) for name, value in among.items()]
        found |= {f"alerted:{name}": value for name, value in among.items()}
    warnings = [n for n, v in found.items() if config.psi.warn <= v < config.psi.action]
    actions = [n for n, v in found.items() if v >= config.psi.action]
    return _Drift(metrics, warnings, actions, False)


def _alert_volume(
    engine: sa.Engine,
    config: MonitoringConfig,
    evaluation: EvaluationConfig,
    start: datetime,
    end: datetime,
) -> list[Metric]:
    with engine.connect() as connection:
        volume = connection.execute(
            sa.select(sa.func.count()).where(alerts.c.raised_at >= start, alerts.c.raised_at < end)
        ).scalar_one()
    budget = evaluation.alert_budget_per_day
    band = config.alert_volume
    return [
        Metric("alert_volume", float(volume)),
        Metric("alert_budget", float(budget)),
        Metric("alert_volume_ratio", volume / budget),
        Metric("alert_volume_ratio_min", band.min_share_of_budget),
        Metric("alert_volume_ratio_max", band.max_share_of_budget),
    ]


def _band_mix(engine: sa.Engine, start: datetime, end: datetime) -> list[Metric]:
    with engine.connect() as connection:
        counts: dict[str, int] = dict(
            tuple(row)
            for row in connection.execute(
                sa.select(risk_assessments.c.risk_band, sa.func.count())
                .where(
                    risk_assessments.c.assessed_at >= start,
                    risk_assessments.c.assessed_at < end,
                    ~risk_assessments.c.degraded,
                )
                .group_by(risk_assessments.c.risk_band)
            )
        )
    total = sum(counts.values())
    found: list[Metric] = []
    for band in BANDS:
        found.append(Metric("risk_band_count", float(counts.get(band, 0)), member=band))
        if total:
            found.append(Metric("risk_band_share", counts.get(band, 0) / total, member=band))
    return found


def _label_performance(
    engine: sa.Engine,
    config: MonitoringConfig,
    evaluation: EvaluationConfig,
    day: date,
    as_of: datetime,
) -> list[Metric]:
    """Precision, recall and false positive rate at High on matured Transactions, overall and by
    segment, against the labels known at `as_of`."""
    # Only Transactions older than the maturity window: a younger one has had too little time to
    # be reported, so being unlabelled says nothing about it.
    cutoff = as_of - timedelta(days=evaluation.label_maturity_days)
    window_start = cutoff - timedelta(days=config.labels.window_days)
    bands = wallet_age_bands(evaluation.wallet_age_band_edges)
    age_days = (
        sa.func.extract("epoch", transactions.c.occurred_at - wallets.c.registered_at) / 86400
    )
    age_band = sa.case(
        *[(age_days < upper, label) for upper, label in bands[:-1]], else_=bands[-1][1]
    ).label("wallet_age_band")
    labelled = fraud_labels.c.transaction_id.is_not(None).label("fraud")
    high = (risk_assessments.c.risk_band == "High").label("high")
    query = (
        sa.select(
            transactions.c.region,
            wallets.c.kyc_tier,
            transactions.c.channel,
            age_band,
            high,
            labelled,
            sa.func.count().label("n"),
        )
        .select_from(transactions)
        .join(risk_assessments, risk_assessments.c.transaction_id == transactions.c.transaction_id)
        .join(wallets, wallets.c.wallet_id == transactions.c.subject_wallet_id)
        .outerjoin(
            fraud_labels,
            sa.and_(
                fraud_labels.c.transaction_id == transactions.c.transaction_id,
                fraud_labels.c.discovered_at <= as_of,
            ),
        )
        .where(transactions.c.occurred_at >= window_start, transactions.c.occurred_at < cutoff)
        .group_by(
            transactions.c.region,
            wallets.c.kyc_tier,
            transactions.c.channel,
            age_band,
            high,
            labelled,
        )
    )
    with engine.connect() as connection:
        frame = pd.DataFrame(
            connection.execute(query).all(),
            columns=["region", "kyc_tier", "channel", "wallet_age_band", "high", "fraud", "n"],
        )
    if frame.empty:
        return []
    frame["n"] = frame["n"].astype(float)
    groups: list[tuple[str, str, pd.DataFrame]] = [(ALL, ALL, frame)]
    for segment in SEGMENTS:
        groups += [(segment, str(m), part) for m, part in frame.groupby(segment)]
    found: list[Metric] = []
    for dimension, member, part in groups:
        found += _confusion_metrics(dimension, member, part)
    return found + _regressions(engine, config, day, found)


def _confusion_metrics(dimension: str, member: str, part: pd.DataFrame) -> list[Metric]:
    def count(high: bool | None, fraud: bool | None) -> float:
        picked = part
        if high is not None:
            picked = picked[picked["high"] == high]
        if fraud is not None:
            picked = picked[picked["fraud"] == fraud]
        return float(picked["n"].sum())

    tp, fp = count(True, True), count(True, False)
    fn, tn = count(False, True), count(False, False)
    found = [
        Metric("label_transactions", tp + fp + fn + tn, dimension, member),
        Metric("label_positives", tp + fn, dimension, member),
    ]
    # A rate with nothing to divide is left out rather than reported as zero.
    for name, numerator, denominator in (
        ("label_precision", tp, tp + fp),
        ("label_recall", tp, tp + fn),
        ("label_fpr", fp, fp + tn),
    ):
        if denominator:
            found.append(Metric(name, numerator / denominator, dimension, member))
    return found


def _regressions(
    engine: sa.Engine, config: MonitoringConfig, day: date, today: list[Metric]
) -> list[Metric]:
    """How far each segment's precision and recall fell below their mean over the previous days
    the job ran, where the segment has enough known Fraud labels to mean anything."""
    positives = {(m.dimension, m.member): m.value for m in today if m.name == "label_positives"}
    reference_start = day - timedelta(days=config.labels.reference_days)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                monitoring_metrics.c.metric,
                monitoring_metrics.c.dimension,
                monitoring_metrics.c.member,
                sa.func.avg(monitoring_metrics.c.value),
            )
            .where(
                monitoring_metrics.c.metric.in_(["label_precision", "label_recall"]),
                monitoring_metrics.c.day >= reference_start,
                monitoring_metrics.c.day < day,
            )
            .group_by(
                monitoring_metrics.c.metric,
                monitoring_metrics.c.dimension,
                monitoring_metrics.c.member,
            )
        ).all()
    reference = {(r[0], r[1], r[2]): float(r[3]) for r in rows}
    found = []
    for m in today:
        if m.name not in ("label_precision", "label_recall"):
            continue
        before = reference.get((m.name, m.dimension, m.member))
        if before is None or before <= 0:
            continue
        if positives.get((m.dimension, m.member), 0.0) < config.labels.min_positives:
            continue
        drop = "label_drop_" + m.name.removeprefix("label_")
        found.append(Metric(drop, (before - m.value) / before, m.dimension, m.member))
    if found:
        found.append(Metric("label_drop_threshold", config.labels.max_relative_drop))
    return found


def _discoveries(
    engine: sa.Engine, config: MonitoringConfig, start: datetime, end: datetime, as_of: datetime
) -> list[Metric]:
    """Where recent Fraud discoveries came from, by source and by Channel, and how many of the
    Fraud-labelled Transactions the system had scored High."""
    window_start = end - timedelta(days=config.discoveries.window_days)
    discovered = fraud_labels.c.discovered_at
    channel = sa.func.coalesce(transactions.c.channel, "unknown")
    visible = sa.and_(discovered >= window_start, discovered < end, discovered <= as_of)
    with engine.connect() as connection:
        by_source: dict[str, int] = dict(
            tuple(row)
            for row in connection.execute(
                sa.select(fraud_labels.c.source, sa.func.count())
                .where(visible)
                .group_by(fraud_labels.c.source)
            )
        )
        by_channel = connection.execute(
            sa.select(channel, sa.func.count(), sa.func.count().filter(discovered >= start))
            .select_from(fraud_labels)
            .outerjoin(transactions, transactions.c.transaction_id == fraud_labels.c.transaction_id)
            .where(visible)
            .group_by(channel)
        ).all()
        scored, flagged = connection.execute(
            sa.select(
                sa.func.count(),
                sa.func.count().filter(risk_assessments.c.risk_band == "High"),
            )
            .select_from(fraud_labels)
            .join(
                risk_assessments, risk_assessments.c.transaction_id == fraud_labels.c.transaction_id
            )
            .where(discovered < end, discovered <= as_of)
        ).one()
    total = sum(by_source.values())
    found = [Metric("discoveries_by_source", float(by_source.get(s, 0)), member=s) for s in SOURCES]
    if total:
        found += [Metric("discovery_share", by_source.get(s, 0) / total, member=s) for s in SOURCES]
    for name, in_window, on_day in by_channel:
        found.append(Metric("discoveries_by_channel", float(in_window), member=name))
        found.append(Metric("discoveries_day", float(on_day), member=name))
    found += [Metric("discovered_scored", float(scored)), Metric("discovered_high", float(flagged))]
    if scored:
        found.append(Metric("recall_discovered", flagged / scored))
    return found


def _store(engine: sa.Engine, day: date, metrics: list[Metric]) -> None:
    with engine.begin() as connection:
        connection.execute(sa.delete(monitoring_metrics).where(monitoring_metrics.c.day == day))
        rows: list[dict[str, Any]] = [
            {
                "day": day,
                "metric": m.name,
                "dimension": m.dimension,
                "member": m.member,
                "value": m.value if np.isfinite(m.value) else 0.0,
            }
            for m in metrics
        ]
        for chunk in itertools.batched(rows, 500):
            connection.execute(insert(monitoring_metrics), list(chunk))
