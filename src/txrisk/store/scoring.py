"""Scoring that records what it scores: idempotent on Transaction ID, raising Alerts for High.

The sync API and the stream workers both use it: whichever path scores a Transaction first
stores the assessment, and every later call, from either path, gets that stored assessment back.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa

from txrisk.features import FeatureConfig, HistoryProvider, Transaction
from txrisk.models.bundle import ModelBundle
from txrisk.observability.tracing import span
from txrisk.scoring import (
    Champion,
    FixedClock,
    InMemoryMetrics,
    Metrics,
    ModelHealth,
    RiskAssessment,
    RiskBand,
    RulesConfig,
    ScoringService,
)
from txrisk.scoring.metrics import ALERTS_RESCORED, RESCORED
from txrisk.store import records
from txrisk.store.history import PostgresHistory


class HistoryCache(Protocol):
    """A cache in front of Postgres for the history the features read: `RedisHistoryCache`."""

    def history(self, connection: sa.Connection) -> HistoryProvider:
        """A history that answers as `PostgresHistory` does, over `connection` as its fallback."""
        ...

    def record(self, connection: sa.Connection, transaction: Transaction) -> None:
        """Add a Transaction that is durably stored; idempotent."""
        ...


@dataclass(frozen=True)
class Scored:
    assessment: RiskAssessment
    # True when an earlier call had already scored the Transaction and this returns its result.
    replayed: bool


@dataclass(frozen=True)
class Rescored:
    """What one re-scoring run did."""

    # Stored assessments replaced by the full model's.
    rescored: int
    # Of those, how many raised an Alert (a Low or Medium that became High).
    alerts_raised: int
    # Degraded assessments still waiting afterwards.
    remaining: int
    # Why the run stopped short of the queue's end, if the model was not healthy.
    stopped: str | None = None


class StoredScoring:
    def __init__(
        self,
        engine: sa.Engine,
        *,
        features: FeatureConfig,
        rules: RulesConfig,
        bundle: ModelBundle | Champion | None = None,
        cache: HistoryCache | None = None,
        model_timeout: float | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        """A bundle is the Champion model; a `Champion` may be unavailable or slow, and then
        Transactions are scored in Degraded mode (see `ScoringService`) and queued for
        re-scoring (`rescore_degraded`). `model_timeout` (seconds) bounds one model call when
        `bundle` is a bare bundle; a `Champion` carries its own. `metrics` defaults to counters
        in this process, read from `.metrics`.

        With a `cache`, the features read their history through it, and every Transaction is
        added to it once stored (and again if it was already stored, which repairs an add that
        failed earlier). Without one, history is read from Postgres itself."""
        self._engine = engine
        self._features = features
        self._rules = rules
        self.metrics = metrics or InMemoryMetrics()
        if isinstance(bundle, ModelBundle):
            bundle = Champion.of(bundle, timeout=model_timeout, metrics=self.metrics)
        self._champion = bundle
        self._cache = cache
        self._more_to_rescore = False

    def assess(self, transaction: Transaction) -> Scored:
        """The Transaction's Risk assessment, scored and stored with the Transaction if it is new.

        The clock is the Transaction's own event time (ADR 0005), so its features see exactly the
        history before it, whenever the call arrives. Only a High Risk band raises an Alert.
        Raises `records.UnknownParty` for unregistered parties, and `records.TransactionConflict`
        if a different Transaction is stored under the same ID.
        """
        with span("score.transaction", transaction_id=transaction.transaction_id):
            return self._assess(transaction)

    def _assess(self, transaction: Transaction) -> Scored:
        with self._engine.connect() as connection:
            with span("store.lookup"):
                stored = self._stored(connection, transaction)
            if stored is not None:
                self._cached(connection, transaction)
                return Scored(stored, replayed=True)
            missing = records.unknown_parties(connection, transaction)
            if missing:
                raise records.UnknownParty(", ".join(missing))
            service = ScoringService(
                self._history(connection),
                FixedClock(transaction.occurred_at),
                features=self._features,
                rules=self._rules,
                bundle=self._champion,
                metrics=self.metrics,
            )
            assessment = service.assess(transaction)

        with span("store.save"), self._engine.begin() as connection:
            # A concurrent call may have stored a Transaction under this ID since the check
            # above: the insert waits for it to commit, and raises `TransactionConflict` if it
            # differs from this one, so a different payload is never answered as a replay.
            records.record_transaction(connection, transaction)
            # A concurrent call for the same Transaction may have stored its assessment first;
            # the insert then waits for it to commit, stores nothing, and its result stands.
            first = records.save_assessment(
                connection, assessment, assessed_at=transaction.occurred_at
            )
            if first:
                records.save_model_inputs(
                    connection, assessment, assessed_at=transaction.occurred_at
                )
            if first and assessment.risk_band is RiskBand.HIGH:
                records.raise_alert(connection, transaction)
            # Read back, so this answer and every replay of it are the same stored assessment.
            stored = records.stored_assessment(connection, transaction.transaction_id)
        assert stored is not None
        # Only now is the Transaction durably stored, which is what the cache may hold.
        with span("cache.record"), self._engine.connect() as connection:
            self._cached(connection, transaction)
        return Scored(stored, replayed=not first)

    def model_health(self) -> ModelHealth:
        """Whether the Champion model is usable (without scoring anything); a rules-only
        service, with no model configured, is not degraded."""
        return ModelHealth(degraded=False) if self._champion is None else self._champion.health()

    def active_bundle(self) -> ModelBundle | None:
        """The Champion model's bundle once loaded; None if none is configured or it is down."""
        return None if self._champion is None else self._champion.loaded

    def awaiting_rescore(self) -> int:
        """How many stored assessments are degraded and waiting to be re-scored."""
        with self._engine.connect() as connection:
            return records.count_awaiting_rescore(connection)

    def rescore_due(self) -> bool:
        """True once after the Champion model recovered (or after a run that left more to do):
        the cue for a worker or the API to call `rescore_degraded`."""
        recovered = self._champion is not None and self._champion.take_recovery()
        due, self._more_to_rescore = recovered or self._more_to_rescore, False
        return due

    def rescore_degraded(self, limit: int | None = None) -> Rescored:
        """Re-score Transactions whose stored assessment is degraded, with the full model, oldest
        first, at most `limit`; stops early if the model is not healthy.

        Each re-scored assessment replaces the stored one (the replaced one is kept in
        `risk_assessment_history`), scored from the history before the Transaction, as it would
        have been. Alerts follow the new band, never the old one, and a Transaction has at most
        one: a Low or Medium that is now High raises its Alert; a High that was degraded has its
        Alert already, so none is added; a High that is now lower keeps its Alert, as an Alert
        records that a Transaction was raised, which a better look does not undo. Safe to run
        concurrently and repeatedly: a replaced assessment is no longer degraded.
        """
        rescored = alerts_raised = 0
        stopped: str | None = None
        if self._champion is None:
            stopped = "no model is configured"
        while stopped is None and (limit is None or rescored < limit):
            wanted = 100 if limit is None else min(100, limit - rescored)
            with self._engine.connect() as connection:
                batch = records.awaiting_rescore(connection, limit=wanted)
            if not batch:
                break
            for transaction_id in batch:
                outcome = self._rescore(transaction_id)
                if isinstance(outcome, str):
                    stopped = outcome
                    break
                if outcome is not None:
                    rescored += 1
                    alerts_raised += outcome
        with self._engine.connect() as connection:
            remaining = records.count_awaiting_rescore(connection)
        self._more_to_rescore = remaining > 0 and stopped is None
        return Rescored(rescored, alerts_raised, remaining, stopped)

    def _rescore(self, transaction_id: str) -> int | str | None:
        """Re-score one Transaction: whether it raised an Alert (0 or 1), None if it was
        not waiting any more, or why the model could not do it."""
        assert self._champion is not None
        with self._engine.connect() as connection:
            transaction = records.stored_transaction(connection, transaction_id)
            if transaction is None:
                return None
            assessment = ScoringService(
                self._history(connection),
                FixedClock(transaction.occurred_at),
                features=self._features,
                rules=self._rules,
                bundle=self._champion,
                metrics=self.metrics,
            ).assess(transaction)
        if assessment.degraded:
            return self._champion.health().reason or "the model is degraded"
        with self._engine.begin() as connection:
            if not records.replace_degraded_assessment(connection, assessment):
                return None
            records.save_model_inputs(connection, assessment, assessed_at=transaction.occurred_at)
            raised = assessment.risk_band is RiskBand.HIGH and records.raise_alert(
                connection, transaction
            )
        self.metrics.increment(RESCORED)
        if raised:
            self.metrics.increment(ALERTS_RESCORED, day=transaction.occurred_at.date().isoformat())
        return int(raised)

    def _history(self, connection: sa.Connection) -> HistoryProvider:
        if self._cache is None:
            return PostgresHistory(connection)
        return self._cache.history(connection)

    def _cached(self, connection: sa.Connection, transaction: Transaction) -> None:
        if self._cache is not None:
            self._cache.record(connection, transaction)

    def record_sim_swap(self, wallet_id: str, occurred_at: datetime) -> bool:
        """Store a SIM swap as history, for the Transactions after it; False if it was already
        stored. Raises `records.UnknownParty` for an unregistered Wallet."""
        with self._engine.begin() as connection:
            if not records.wallet_exists(connection, wallet_id):
                raise records.UnknownParty(f"wallet_id={wallet_id}")
            return records.add_sim_swap(connection, wallet_id, occurred_at)

    @staticmethod
    def _stored(connection: sa.Connection, transaction: Transaction) -> RiskAssessment | None:
        stored = records.stored_assessment(connection, transaction.transaction_id)
        if stored is not None:
            recorded = records.stored_transaction(connection, transaction.transaction_id)
            if recorded != transaction:
                raise records.TransactionConflict(transaction.transaction_id)
        return stored
