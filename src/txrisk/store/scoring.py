"""Scoring that records what it scores: idempotent on Transaction ID, raising Alerts for High.

The sync API uses it, and the stream workers are meant to: whichever path scores a Transaction
first stores the assessment, and every later call gets that stored assessment back.
"""

from dataclasses import dataclass

import sqlalchemy as sa

from txrisk.features import FeatureConfig, Transaction
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import FixedClock, RiskAssessment, RiskBand, RulesConfig, ScoringService
from txrisk.store import records
from txrisk.store.history import PostgresHistory


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
    ) -> None:
        self._engine = engine
        self._features = features
        self._rules = rules
        self._bundle = bundle

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
                return Scored(stored, replayed=True)
            missing = records.unknown_parties(connection, transaction)
            if missing:
                raise records.UnknownParty(", ".join(missing))
            service = ScoringService(
                PostgresHistory(connection),
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
        return Scored(stored, replayed=not first)

    @staticmethod
    def _stored(connection: sa.Connection, transaction: Transaction) -> RiskAssessment | None:
        stored = records.stored_assessment(connection, transaction.transaction_id)
        if stored is not None:
            recorded = records.stored_transaction(connection, transaction.transaction_id)
            if recorded != transaction:
                raise records.TransactionConflict(transaction.transaction_id)
        return stored
