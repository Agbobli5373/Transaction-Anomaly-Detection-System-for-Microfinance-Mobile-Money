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
from txrisk.scoring import FixedClock, RiskAssessment, RiskBand, RulesConfig, ScoringService
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


class StoredScoring:
    def __init__(
        self,
        engine: sa.Engine,
        *,
        features: FeatureConfig,
        rules: RulesConfig,
        bundle: ModelBundle | None = None,
        cache: HistoryCache | None = None,
    ) -> None:
        """With a `cache`, the features read their history through it, and every Transaction is
        added to it once stored (and again if it was already stored, which repairs an add that
        failed earlier). Without one, history is read from Postgres itself."""
        self._engine = engine
        self._features = features
        self._rules = rules
        self._bundle = bundle
        self._cache = cache

    def assess(self, transaction: Transaction) -> Scored:
        """The Transaction's Risk assessment, scored and stored with the Transaction if it is new.

        The clock is the Transaction's own event time (ADR 0005), so its features see exactly the
        history before it, whenever the call arrives. Only a High Risk band raises an Alert.
        Raises `records.UnknownParty` for unregistered parties, and `records.TransactionConflict`
        if a different Transaction is stored under the same ID.
        """
        with self._engine.connect() as connection:
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
                bundle=self._bundle,
            )
            assessment = service.assess(transaction)

        with self._engine.begin() as connection:
            # A concurrent call may have stored a Transaction under this ID since the check
            # above: the insert waits for it to commit, and raises `TransactionConflict` if it
            # differs from this one, so a different payload is never answered as a replay.
            records.record_transaction(connection, transaction)
            # A concurrent call for the same Transaction may have stored its assessment first;
            # the insert then waits for it to commit, stores nothing, and its result stands.
            first = records.save_assessment(
                connection, assessment, assessed_at=transaction.occurred_at
            )
            if first and assessment.risk_band is RiskBand.HIGH:
                records.raise_alert(connection, transaction)
            # Read back, so this answer and every replay of it are the same stored assessment.
            stored = records.stored_assessment(connection, transaction.transaction_id)
        assert stored is not None
        # Only now is the Transaction durably stored, which is what the cache may hold.
        with self._engine.connect() as connection:
            self._cached(connection, transaction)
        return Scored(stored, replayed=not first)

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
