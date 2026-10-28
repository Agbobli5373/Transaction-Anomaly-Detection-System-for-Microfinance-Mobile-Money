"""Degraded mode in tests: a hand-built world in Postgres, and a model bundle that can be told to
be slow, to fail, or to score as the test wants."""

import dataclasses
import time
from types import SimpleNamespace
from typing import Any, ClassVar

import pandas as pd
from scoring_helpers import REGISTERED, SUBJECT, routine, tx

from txrisk.evaluation.thresholds import BandThresholds
from txrisk.features import Transaction
from txrisk.scoring.reasons import INPUT_REASONS
from txrisk.store import create_engine, records

FRIEND = "w-friend"
STRANGER = "w-stranger"
AGENT = "A00001"

# The stub's Risk band thresholds, on the stacked scale (a probability x 100) that a bundle uses.
HIGH = 17.0
MEDIUM = 5.0


def on_app(transaction: Transaction) -> Transaction:
    return dataclasses.replace(transaction, channel="app")


def drain(at: Any) -> Transaction:
    """A Transfer of 90% of the balance to a stranger: a Critical rule hit after a SIM swap."""
    return on_app(tx(at, amount=900.0, counterparty=STRANGER, balance=1000.0))


def populate(database: str) -> None:
    """Three Wallets, an Agent and the Subject wallet's routine, in the database."""
    engine = create_engine(database)
    with engine.begin() as connection:
        for wallet in (SUBJECT, FRIEND, STRANGER):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )
        records.add_agent(connection, AGENT, region="Ashanti")
        for t in map(on_app, routine()):
            records.record_transaction(connection, t)
    engine.dispose()


class StubBundle:
    """Stands in for a `ModelBundle`: scores each Transaction as `scores` says (else `default`,
    on the stacked scale), after `delay` seconds, or raises when `fail` is set."""

    version = "stub-1"
    thresholds = BandThresholds(medium=MEDIUM, high=HIGH)
    stacker = SimpleNamespace(weights={"rules": 1.0})
    fitted_on: ClassVar[dict[str, Any]] = {}

    def __init__(self, default: float = 1.0, *, delay: float = 0.0, fail: bool = False) -> None:
        self.default = default
        self.scores: dict[str, float] = {}
        self.delay = delay
        self.fail = fail
        self.calls = 0

    def components(self, inputs: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
        self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("the model broke")
        return pd.DataFrame({"anomaly": [0.1], "fraud_probability": [0.2]}, index=inputs.index)

    def risk_scores(
        self, inputs: pd.DataFrame, rules: pd.DataFrame, components: pd.DataFrame | None = None
    ) -> pd.Series:
        return pd.Series([self.scores.get(inputs.index[0], self.default)], index=inputs.index)

    def contributions(self, inputs: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame(0.0, index=inputs.index, columns=list(INPUT_REASONS))


class Monotonic:
    """The circuit breaker's clock, moved on by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
