"""The scoring service: one entry point that turns a Transaction into a Risk assessment."""

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from txrisk.features import FeatureConfig, HistoryProvider, Transaction, transaction_features
from txrisk.models import inputs_for
from txrisk.models.anomaly import AnomalyModel
from txrisk.models.supervised import SupervisedModel
from txrisk.scoring.config import RulesConfig, Severity
from txrisk.scoring.rules import RuleHit, rule_hits

RULES_ONLY = "rules-only"


class RiskBand(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    value: float | str


@dataclass(frozen=True)
class RiskAssessment:
    transaction_id: str
    risk_score: int
    risk_band: RiskBand
    reasons: tuple[Reason, ...]
    # Each component on a 0-1 scale; components without a model yet are None.
    component_scores: dict[str, float | None] = field(default_factory=dict)
    model_version: str = RULES_ONLY
    degraded: bool = False


class Clock(Protocol):
    """The simulation clock (ADR 0005): business logic never reads the wall clock."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class FixedClock:
    at: datetime

    def now(self) -> datetime:
        return self.at


def _band(score: float, config: RulesConfig) -> RiskBand:
    if score >= config.bands.high:
        return RiskBand.HIGH
    if score >= config.bands.medium:
        return RiskBand.MEDIUM
    return RiskBand.LOW


def _reasons(hits: list[RuleHit], config: RulesConfig) -> tuple[Reason, ...]:
    """Critical hits first, then the heaviest; rule-set order breaks ties."""
    weight = config.severity_weights
    ranked = sorted(
        enumerate(hits),
        key=lambda h: (h[1].severity != Severity.CRITICAL, -weight[h[1].severity], h[0]),
    )
    return tuple(Reason(h.code, h.message, h.value) for _, h in ranked[: config.max_reasons])


class ScoringService:
    def __init__(
        self,
        history: HistoryProvider,
        clock: Clock,
        *,
        features: FeatureConfig,
        rules: RulesConfig,
        anomaly: AnomalyModel | None = None,
        supervised: SupervisedModel | None = None,
    ) -> None:
        self._history = history
        self._clock = clock
        self._features = features
        self._rules = rules
        self._anomaly = anomaly
        self._supervised = supervised

    def assess(self, transaction: Transaction) -> RiskAssessment:
        features = transaction_features(
            transaction, self._history, now=self._clock.now(), config=self._features
        )
        hits = rule_hits(transaction, features, self._rules.rules)
        weights = self._rules.severity_weights
        rules_score = 1 - math.prod(1 - weights[h.severity] for h in hits)
        score = 100 * rules_score
        if any(h.severity == Severity.CRITICAL for h in hits):
            score = max(score, self._rules.critical_floor)
        risk_score = round(score)
        anomaly = supervised = None
        if self._anomaly is not None or self._supervised is not None:
            inputs = inputs_for(
                transaction, features
            )  # built once: it costs more than either model
            if self._anomaly is not None:
                anomaly = float(self._anomaly.score(inputs).iloc[0])
            if self._supervised is not None:
                supervised = float(self._supervised.score(inputs).iloc[0])
        return RiskAssessment(
            transaction_id=transaction.transaction_id,
            risk_score=risk_score,
            risk_band=_band(risk_score, self._rules),
            reasons=_reasons(hits, self._rules),
            component_scores={"rules": rules_score, "anomaly": anomaly, "supervised": supervised},
        )
