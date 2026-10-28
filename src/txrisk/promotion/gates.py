"""The two gates a Challenger model must pass before it may be promoted.

**Backtest gate.** The Champion and the Challenger both score the most recent matured
Transactions, from the model inputs the scoring path stored for each (so nothing is recomputed
and both see exactly what the Champion saw), and are judged against the Fraud labels known at the
simulated time. Ground truth is never read (ADR 0007), and a Transaction counts only once older
than the Label maturity window (ADR 0008), as in monitoring's label-based performance. The
Challenger passes if it matches or beats the Champion on PR-AUC and on precision at the Alert
budget, and no segment's false positive rate at High rises by more than the configured share.

**Shadow gate.** What the Challenger did on live traffic, from `challenger_scores`: long enough in
shadow, its simulated Alert volume close to the Alert budget, its latency within the target, and
no scoring errors.

Each gate returns a report that says why it failed, so a refused Promotion is explained, and that
is kept with the Promotion when it is allowed.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import sqlalchemy as sa

from txrisk.evaluation import EvaluationConfig
from txrisk.evaluation.metrics import at_threshold, precision_at_budget, ranking
from txrisk.models.bundle import ModelBundle
from txrisk.models.inputs import INPUT_COLUMNS
from txrisk.promotion.config import BacktestConfig, PromotionConfig, ShadowConfig
from txrisk.scoring.config import RulesConfig, Severity
from txrisk.store.tables import (
    challenger_scores,
    fraud_labels,
    model_inputs,
    risk_assessments,
    transactions,
    wallets,
)

SEGMENTS = ("region", "kyc_tier", "channel", "wallet_age_band")


@dataclass(frozen=True)
class ModelResult:
    """How one model did on the backtest window."""

    pr_auc: float | None
    precision_at_budget: float | None


@dataclass(frozen=True)
class SegmentResult:
    """The false positive rate at High of the two models in one segment."""

    segment: str
    member: str
    # Legitimate Transactions in the segment: what the rate is a share of.
    negatives: int
    champion_fpr: float | None
    challenger_fpr: float | None
    # False when the segment has too few legitimate Transactions to count.
    judged: bool
    passed: bool


@dataclass(frozen=True)
class BacktestReport:
    passed: bool
    # Why not, one sentence each; empty when it passed.
    failures: list[str]
    window_start: datetime
    window_end: datetime
    transactions: int
    positives: int
    champion: ModelResult | None = None
    challenger: ModelResult | None = None
    segments: list[SegmentResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        report: dict[str, Any] = _json(asdict(self))
        return report


@dataclass(frozen=True)
class ShadowReport:
    passed: bool
    failures: list[str]
    scored: int
    errors: int
    # Simulated days between the first and the latest shadow score.
    days: float
    high: int
    # The Challenger's High a simulated day, to compare with the Alert budget.
    alerts_per_day: float | None
    alert_budget_per_day: int
    p95_latency_ms: float | None

    def to_json(self) -> dict[str, Any]:
        report: dict[str, Any] = _json(asdict(self))
        return report


@dataclass(frozen=True)
class GateReport:
    version: str
    champion_version: str
    backtest: BacktestReport
    shadow: ShadowReport

    @property
    def passed(self) -> bool:
        return self.backtest.passed and self.shadow.passed

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "champion_version": self.champion_version,
            "passed": self.passed,
            "backtest": self.backtest.to_json(),
            "shadow": self.shadow.to_json(),
        }


def _json(value: Any) -> Any:
    """`value` with datetimes as ISO strings and NaN as null, for the audit log and responses."""
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def critical_rules(rules: RulesConfig) -> set[str]:
    """The codes of the rules whose hit forces a High Risk band."""
    configured = type(rules.rules).model_fields.keys() - {"disabled"}
    return {
        code
        for code in configured
        if code not in rules.rules.disabled
        and getattr(rules.rules, code).severity is Severity.CRITICAL
    }


@dataclass(frozen=True)
class MaturedWindow:
    """What the backtest scores: the stored model inputs and Rule scores of the matured
    Transactions in the window, and the labels known about them."""

    start: datetime
    end: datetime
    inputs: pd.DataFrame
    # rules_score and critical, as the stacker takes them, on the same index.
    rules: pd.DataFrame
    # fraud (a known Fraud label), day and a column per segment, on the same index.
    frame: pd.DataFrame


def matured_window(
    engine: sa.Engine,
    *,
    rules: RulesConfig,
    config: BacktestConfig,
    evaluation: EvaluationConfig,
    as_of: datetime,
) -> MaturedWindow:
    """The latest window of Transactions old enough to have mature labels at `as_of` (the
    simulated time), with the Fraud labels discovered by then. Only Transactions a model scored
    are in it: a Degraded assessment stored no inputs."""
    from txrisk.evaluation.report import _wallet_age_band

    end = as_of - timedelta(days=evaluation.label_maturity_days)
    start = end - timedelta(days=config.window_days)
    query = (
        sa.select(
            transactions.c.transaction_id,
            transactions.c.occurred_at,
            transactions.c.region,
            transactions.c.channel,
            wallets.c.kyc_tier,
            wallets.c.registered_at,
            model_inputs.c.inputs,
            risk_assessments.c.component_scores["rules"].as_float().label("rules_score"),
            risk_assessments.c.reasons,
            fraud_labels.c.transaction_id.is_not(None).label("fraud"),
        )
        .select_from(transactions)
        .join(model_inputs, model_inputs.c.transaction_id == transactions.c.transaction_id)
        .join(risk_assessments, risk_assessments.c.transaction_id == transactions.c.transaction_id)
        .join(wallets, wallets.c.wallet_id == transactions.c.subject_wallet_id)
        .outerjoin(
            fraud_labels,
            sa.and_(
                fraud_labels.c.transaction_id == transactions.c.transaction_id,
                fraud_labels.c.discovered_at <= as_of,
            ),
        )
        .where(transactions.c.occurred_at >= start, transactions.c.occurred_at < end)
        .order_by(transactions.c.occurred_at, transactions.c.transaction_id)
    )
    with engine.connect() as connection:
        found = connection.execute(query).all()
    index = pd.Index([r.transaction_id for r in found], name="transaction_id")
    inputs = pd.DataFrame([r.inputs for r in found], index=index).reindex(columns=INPUT_COLUMNS)
    critical = critical_rules(rules)
    scored = pd.DataFrame(
        {
            "rules_score": [r.rules_score for r in found],
            "critical": [any(h["code"] in critical for h in r.reasons) for r in found],
        },
        index=index,
    )
    frame = pd.DataFrame(
        {
            "occurred_at": pd.to_datetime([r.occurred_at for r in found], utc=True),
            "region": [r.region for r in found],
            "kyc_tier": [r.kyc_tier for r in found],
            "channel": [r.channel for r in found],
            "registered_at": pd.to_datetime([r.registered_at for r in found], utc=True),
            "fraud": [bool(r.fraud) for r in found],
        },
        index=index,
    )
    age = (frame["occurred_at"] - frame["registered_at"]).dt.total_seconds() / 86_400
    frame["wallet_age_band"] = _wallet_age_band(age, evaluation.wallet_age_band_edges)
    frame["day"] = frame["occurred_at"].dt.floor("D")
    return MaturedWindow(
        start=start,
        end=end,
        inputs=inputs.astype(float),
        rules=scored.astype({"rules_score": float, "critical": bool}),
        frame=frame,
    )


def _score(bundle: ModelBundle, window: MaturedWindow) -> pd.Series:
    return bundle.risk_scores(window.inputs, window.rules)


def backtest_gate(
    window: MaturedWindow,
    *,
    champion: ModelBundle,
    challenger: ModelBundle,
    config: BacktestConfig,
    evaluation: EvaluationConfig,
) -> BacktestReport:
    """Whether `challenger` matches or beats `champion` on `window` (see the module docstring)."""
    frame = window.frame
    positives = int(frame["fraud"].sum())
    base: dict[str, Any] = {
        "window_start": window.start,
        "window_end": window.end,
        "transactions": len(frame),
        "positives": positives,
    }
    if positives < config.min_positives or positives == len(frame):
        return BacktestReport(
            passed=False,
            failures=[
                f"the window has {positives} known Fraud labels in {len(frame)} Transactions; "
                f"at least {config.min_positives} (and some legitimate) are needed to tell the "
                "models apart"
            ],
            **base,
        )
    scores = {"champion": _score(champion, window), "challenger": _score(challenger, window)}
    results = {
        name: ModelResult(
            pr_auc=ranking(frame["fraud"], score).pr_auc,
            precision_at_budget=precision_at_budget(
                frame["fraud"], score, frame["day"], budget_per_day=evaluation.alert_budget_per_day
            ).precision,
        )
        for name, score in scores.items()
    }
    failures: list[str] = []
    for metric, label in (
        ("pr_auc", "PR-AUC"),
        ("precision_at_budget", "precision at the Alert budget"),
    ):
        champion_value = getattr(results["champion"], metric)
        challenger_value = getattr(results["challenger"], metric)
        if challenger_value is None or champion_value is None or challenger_value < champion_value:
            failures.append(
                f"{label} is {_show(challenger_value)} against "
                f"the Champion's {_show(champion_value)}"
            )
    segments = _segment_results(window, scores, champion, challenger, config)
    for result in segments:
        if not result.passed:
            failures.append(
                f"false positive rate in {result.segment} {result.member} is "
                f"{_show(result.challenger_fpr)} against the Champion's "
                f"{_show(result.champion_fpr)}, "
                f"a rise of more than {config.max_segment_fpr_rise:.0%}"
            )
    return BacktestReport(
        passed=not failures,
        failures=failures,
        champion=results["champion"],
        challenger=results["challenger"],
        segments=segments,
        **base,
    )


def _show(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.4g}"


def _segment_results(
    window: MaturedWindow,
    scores: dict[str, pd.Series],
    champion: ModelBundle,
    challenger: ModelBundle,
    config: BacktestConfig,
) -> list[SegmentResult]:
    """Each segment's false positive rate at High, for both models. A rise from nothing counts as
    a rise: the Challenger may not flag legitimate Transactions the Champion left alone."""
    frame = window.frame
    high = {
        "champion": scores["champion"] >= champion.thresholds.high,
        "challenger": scores["challenger"] >= challenger.thresholds.high,
    }
    found: list[SegmentResult] = []
    for segment in SEGMENTS:
        for member, rows in frame.groupby(segment, sort=True):
            fpr = {
                name: at_threshold(rows["fraud"], flagged[rows.index]).false_positive_rate
                for name, flagged in high.items()
            }
            negatives = int((~rows["fraud"]).sum())
            judged = negatives >= config.min_segment_negatives
            before, after = fpr["champion"], fpr["challenger"]
            rose = (
                before is not None
                and after is not None
                and after > before * (1 + config.max_segment_fpr_rise)
            )
            found.append(
                SegmentResult(
                    segment=segment,
                    member=str(member),
                    negatives=negatives,
                    champion_fpr=before,
                    challenger_fpr=after,
                    judged=judged,
                    passed=not (judged and rose),
                )
            )
    return found


def shadow_gate(
    engine: sa.Engine, *, version: str, config: ShadowConfig, evaluation: EvaluationConfig
) -> ShadowReport:
    """Whether the Challenger `version` has been in shadow long enough, safely and at a
    sensible volume (see the module docstring)."""
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(
                sa.func.count().label("scored"),
                sa.func.count().filter(challenger_scores.c.error.is_not(None)).label("errors"),
                sa.func.count().filter(challenger_scores.c.risk_band == "High").label("high"),
                sa.func.min(challenger_scores.c.assessed_at).label("first"),
                sa.func.max(challenger_scores.c.assessed_at).label("last"),
                sa.func.percentile_cont(0.95)
                .within_group(challenger_scores.c.latency_ms)
                .label("p95"),
            ).where(challenger_scores.c.model_version == version)
        ).one()
    budget = evaluation.alert_budget_per_day
    days = 0.0 if row.scored == 0 else (row.last - row.first).total_seconds() / 86_400
    per_day = row.high / days if days > 0 else None
    p95 = None if row.p95 is None else float(row.p95)
    failures: list[str] = []
    if row.scored == 0:
        failures.append("it has scored no Transactions in shadow")
    elif days < config.min_days:
        failures.append(
            f"it has been in shadow for {days:.1f} simulated days; at least {config.min_days:g} "
            "are needed"
        )
    if row.errors:
        failures.append(f"it failed to score {row.errors} Transaction(s); none are allowed")
    if per_day is not None and abs(per_day - budget) > config.volume_tolerance * budget:
        failures.append(
            f"its simulated Alert volume is {per_day:.1f} a day, outside "
            f"{config.volume_tolerance:.0%} of the Alert budget of {budget}"
        )
    if p95 is not None and p95 > config.p95_latency_ms:
        failures.append(
            f"its p95 scoring latency is {p95:.1f} ms against a target of "
            f"{config.p95_latency_ms:g} ms"
        )
    return ShadowReport(
        passed=not failures,
        failures=failures,
        scored=int(row.scored),
        errors=int(row.errors),
        days=days,
        high=int(row.high),
        alerts_per_day=per_day,
        alert_budget_per_day=budget,
        p95_latency_ms=p95,
    )


def check_gates(
    engine: sa.Engine,
    *,
    champion: ModelBundle,
    challenger: ModelBundle,
    rules: RulesConfig,
    config: PromotionConfig,
    evaluation: EvaluationConfig,
    as_of: datetime,
) -> GateReport:
    """Both gates for `challenger` against `champion`, at the simulated time `as_of`."""
    window = matured_window(
        engine, rules=rules, config=config.backtest, evaluation=evaluation, as_of=as_of
    )
    return GateReport(
        version=challenger.version,
        champion_version=champion.version,
        backtest=backtest_gate(
            window,
            champion=champion,
            challenger=challenger,
            config=config.backtest,
            evaluation=evaluation,
        ),
        shadow=shadow_gate(
            engine, version=challenger.version, config=config.shadow, evaluation=evaluation
        ),
    )
