"""The scoring service: one entry point that turns a Transaction into a Risk assessment."""

import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import pandas as pd

from txrisk.features import (
    FeatureConfig,
    HistoryProvider,
    Transaction,
    TransactionFeatures,
    transaction_features,
)
from txrisk.models import inputs_for
from txrisk.models.anomaly import AnomalyModel
from txrisk.models.supervised import SupervisedModel
from txrisk.scoring.champion import Champion, ModelTimeout
from txrisk.scoring.config import RulesConfig, Severity
from txrisk.scoring.metrics import DEGRADED_ASSESSMENTS, Metrics
from txrisk.scoring.reasons import Reason, rule_reasons, stacked_reasons
from txrisk.scoring.rules import RuleHit, rule_hits

if TYPE_CHECKING:  # the bundle module itself scores rules, so importing it here would cycle
    from txrisk.models.bundle import ModelBundle

log = logging.getLogger(__name__)

RULES_ONLY = "rules-only"


class RiskBand(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


@dataclass(frozen=True)
class RiskAssessment:
    transaction_id: str
    # 0-100. Rules-only scores are whole numbers; a stacked score is the chance of discovered
    # fraud x 100, which is mostly well under 1, so it keeps two decimals.
    risk_score: float
    risk_band: RiskBand
    reasons: tuple[Reason, ...]
    # Each component on a 0-1 scale; components without a model yet are None.
    component_scores: dict[str, float | None] = field(default_factory=dict)
    # Which scale `risk_score` and `risk_band` are on: a bundle version means the stacked scale
    # (the chance of discovered fraud x 100, High near the bundle's threshold, about 0.2), and
    # `rules-only` the rules scale (High from `bands.high` in config/rules.toml, 50). A degraded
    # assessment is `rules-only` too: the Champion model was down, so it is on the rules scale.
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


def _band(score: float, *, medium: float, high: float) -> RiskBand:
    if score >= high:
        return RiskBand.HIGH
    if score >= medium:
        return RiskBand.MEDIUM
    return RiskBand.LOW


def combine_hits(hits: list[RuleHit], config: RulesConfig) -> tuple[float, bool]:
    """The rules score (0-1) of these Rule hits, and whether any is a Critical rule."""
    weights = config.severity_weights
    score = 1 - math.prod(1 - weights[h.severity] for h in hits)
    return score, any(h.severity == Severity.CRITICAL for h in hits)


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
        bundle: "ModelBundle | Champion | None" = None,
        metrics: Metrics | None = None,
    ) -> None:
        """Without a `bundle` the rules alone set the Risk score, and `anomaly` and `supervised`
        are only reported as components. With one, its stacked Risk score, Risk bands and
        Reasons are used, and it reports every component itself.

        A `Champion` can be down or slow: then the assessment is made from Rule hits alone and
        marked degraded (Degraded mode), on the rules scale and bands. A bare bundle is wrapped
        in a Champion with no timeout, so it only degrades if scoring with it raises."""
        self._history = history
        self._clock = clock
        self._features = features
        self._rules = rules
        self._anomaly = anomaly
        self._supervised = supervised
        if bundle is not None and not isinstance(bundle, Champion):
            bundle = Champion.of(bundle)
        self._champion = bundle
        self._metrics = metrics

    def assess(self, transaction: Transaction) -> RiskAssessment:
        features = transaction_features(
            transaction, self._history, now=self._clock.now(), config=self._features
        )
        hits = rule_hits(transaction, features, self._rules.rules)
        rules_score, critical = combine_hits(hits, self._rules)
        if self._champion is not None:
            return self._with_champion(
                self._champion, transaction, features, hits, rules_score, critical
            )
        return self._rules_only(transaction, features, hits, rules_score, critical)

    def _with_champion(
        self,
        champion: Champion,
        transaction: Transaction,
        features: TransactionFeatures,
        hits: list[RuleHit],
        rules_score: float,
        critical: bool,
    ) -> RiskAssessment:
        """The stacked assessment, or the rules-only one marked degraded if the Champion model
        is unavailable, times out or raises."""
        bundle, reason = champion.acquire()
        if bundle is not None:
            try:
                assessment = champion.call(
                    lambda: self._stacked(
                        bundle, transaction, features, hits, rules_score, critical
                    )
                )
            except ModelTimeout as e:
                champion.failed(str(e))
                reason, kind = str(e), "timeout"
            except Exception as e:  # any failure of the model must not stop scoring
                log.warning(
                    "scoring %s with the model failed", transaction.transaction_id, exc_info=True
                )
                reason = f"{type(e).__name__}: {e}"
                champion.failed(reason)
                kind = "error"
            else:
                champion.succeeded()
                return assessment
        else:
            kind = "unavailable"
        if self._metrics is not None:
            self._metrics.increment(DEGRADED_ASSESSMENTS, reason=kind)
        log.warning("degraded mode (%s): %s", kind, reason)
        rules_only = self._rules_only(transaction, features, hits, rules_score, critical)
        return replace(rules_only, degraded=True)

    def _rules_only(
        self,
        transaction: Transaction,
        features: TransactionFeatures,
        hits: list[RuleHit],
        rules_score: float,
        critical: bool,
    ) -> RiskAssessment:
        score = 100 * rules_score
        if critical:
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
            risk_band=_band(
                risk_score, medium=self._rules.bands.medium, high=self._rules.bands.high
            ),
            reasons=rule_reasons(hits, self._rules),
            component_scores={"rules": rules_score, "anomaly": anomaly, "supervised": supervised},
        )

    def _stacked(
        self,
        bundle: "ModelBundle",
        transaction: Transaction,
        features: TransactionFeatures,
        hits: list[RuleHit],
        rules_score: float,
        critical: bool,
    ) -> RiskAssessment:
        inputs = inputs_for(transaction, features)
        rules = pd.DataFrame({"rules_score": [rules_score], "critical": [critical]}, inputs.index)
        components = bundle.components(inputs, rules)
        score = float(bundle.risk_scores(inputs, rules, components).iloc[0])
        reasons = stacked_reasons(
            hits,
            rules_contribution=bundle.stacker.weights["rules"] * rules_score,
            inputs=inputs.iloc[0],
            contributions=bundle.contributions(inputs).iloc[0],
            config=self._rules,
        )
        thresholds = bundle.thresholds
        return RiskAssessment(
            transaction_id=transaction.transaction_id,
            risk_score=round(score, 2),
            # Banded before rounding, as the thresholds were set on unrounded scores.
            risk_band=_band(score, medium=thresholds.medium, high=thresholds.high),
            reasons=reasons,
            component_scores={
                "rules": rules_score,
                "anomaly": float(components["anomaly"].iloc[0]),
                "supervised": float(components["fraud_probability"].iloc[0]),
            },
            model_version=bundle.version,
        )
