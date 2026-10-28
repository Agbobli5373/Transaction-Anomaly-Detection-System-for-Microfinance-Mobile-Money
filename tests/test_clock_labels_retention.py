"""The simulation clock, Fraud labels and the retention purge, against real Postgres."""

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from scoring_helpers import DAY, HOUR, NOW, REGISTERED, SUBJECT, tx

from txrisk.cli import main
from txrisk.scoring import RiskAssessment, RiskBand
from txrisk.store import (
    ClockNotStarted,
    FraudLabels,
    RetentionConfig,
    SimulationClock,
    create_engine,
    load_retention_config,
    purge,
    records,
)

RETENTION_CONFIG = Path(__file__).resolve().parents[1] / "config" / "retention.toml"
FRIEND = "w-friend"


def retention(**days: int) -> RetentionConfig:
    """The shipped retention periods, with any of them replaced."""
    shipped = load_retention_config(RETENTION_CONFIG).days.model_dump()
    return RetentionConfig.model_validate({"days": {**shipped, **days}})


def test_the_clock_has_no_reading_before_any_event(database: str):
    clock = SimulationClock(create_engine(database))

    assert clock.peek() is None
    with pytest.raises(ClockNotStarted):
        clock.now()


def test_the_clock_follows_the_latest_event_time_and_never_goes_back(database: str):
    clock = SimulationClock(create_engine(database))

    assert clock.advance(NOW) is True
    assert clock.advance(NOW + HOUR) is True
    assert clock.advance(NOW) is False  # an older event, delivered late
    assert clock.advance(NOW + HOUR) is False

    assert clock.now() == NOW + HOUR


def test_two_workers_share_one_clock(database: str):
    first, second = (SimulationClock(create_engine(database)) for _ in range(2))

    first.advance(NOW + DAY)
    second.advance(NOW)

    assert second.now() == NOW + DAY


def test_a_label_is_known_only_from_its_discovery_time(database: str):
    labels = FraudLabels(create_engine(database))
    labels.record("T1", NOW + 2 * DAY, "complaint")
    labels.record("T2", NOW + 5 * DAY, "chargeback")

    assert labels.known_at(NOW + DAY) == []
    assert [label.transaction_id for label in labels.known_at(NOW + 2 * DAY)] == ["T1"]
    assert [label.transaction_id for label in labels.known_at(NOW + 6 * DAY)] == ["T1", "T2"]


def test_a_label_stored_early_stays_invisible_until_its_event_time(database: str):
    """Stored well ahead of the clock (a discovery arriving before the Transactions around it
    are consumed), it is still not known at the clock's reading."""
    engine = create_engine(database)
    clock, labels = SimulationClock(engine), FraudLabels(engine)
    clock.advance(NOW)

    labels.record("T1", NOW + 3 * DAY, "complaint")

    assert labels.known_at(clock.now()) == []
    clock.advance(NOW + 3 * DAY)
    assert [label.transaction_id for label in labels.known_at(clock.now())] == ["T1"]


def test_the_first_discovery_of_a_transaction_stands(database: str):
    labels = FraudLabels(create_engine(database))

    assert labels.record("T1", NOW, "complaint") is True
    assert labels.record("T1", NOW, "complaint") is False  # a redelivered event
    assert labels.record("T1", NOW + DAY, "case") is False  # a later discovery

    [label] = labels.known_at(NOW + 9 * DAY)
    assert (label.discovered_at, label.source) == (NOW, "complaint")


def test_a_label_needs_no_stored_transaction(database: str):
    labels = FraudLabels(create_engine(database))

    assert labels.record("T-not-stored-yet", NOW, "chargeback") is True


def _stored(engine: sa.Engine, at, *, band: RiskBand = RiskBand.LOW) -> str:
    """A Transaction at `at` with its Risk assessment, and an Alert if its band is High."""
    transaction = dataclasses.replace(tx(at), channel="app")
    with engine.begin() as connection:
        records.record_transaction(connection, transaction)
        records.save_assessment(
            connection,
            RiskAssessment(
                transaction_id=transaction.transaction_id,
                risk_score=90.0 if band is RiskBand.HIGH else 5.0,
                risk_band=band,
                reasons=(),
            ),
            assessed_at=at,
        )
        if band is RiskBand.HIGH:
            records.raise_alert(connection, transaction)
    return transaction.transaction_id


def _count(engine: sa.Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def _wallets(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        for wallet in (SUBJECT, FRIEND):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )


@pytest.fixture
def stocked(database: str) -> sa.Engine:
    """Parties, and one Transaction a day for 40 days ending at NOW, every fifth with an Alert."""
    engine = create_engine(database)
    _wallets(engine)
    for day in range(40, 0, -1):
        _stored(engine, NOW - day * DAY, band=RiskBand.HIGH if day % 5 == 0 else RiskBand.LOW)
    SimulationClock(engine).advance(NOW)
    return engine


def test_purge_deletes_what_is_older_than_each_tables_retention(stocked: sa.Engine):
    with stocked.begin() as connection:
        records.add_sim_swap(connection, SUBJECT, NOW - 30 * DAY)
        records.add_sim_swap(connection, SUBJECT, NOW - 5 * DAY)
    labels = FraudLabels(stocked)
    labels.record("T-old", NOW - 30 * DAY, "complaint")
    labels.record("T-new", NOW - 5 * DAY, "complaint")

    purged = purge(
        stocked,
        SimulationClock(stocked),
        retention(
            transactions=20,
            risk_assessments=20,
            alerts=10,
            sim_swaps=7,
            fraud_labels=15,
            audit_log=400,
        ),
    )

    assert purged is not None and purged.at == NOW
    # One Transaction and assessment a day, the oldest 40 days back: 20 remain after day 20.
    assert purged.deleted["transactions"] == 20
    assert purged.deleted["risk_assessments"] == 20
    assert _count(stocked, "transactions") == _count(stocked, "risk_assessments") == 20
    # Alerts were raised 40, 35, ... 5 days back; those 10 days back or less are kept.
    assert _count(stocked, "alerts") == 2
    assert _count(stocked, "sim_swaps") == 1
    assert [label.transaction_id for label in labels.known_at(NOW)] == ["T-new"]


def test_purge_deletes_old_challenger_scores_by_event_time(stocked: sa.Engine):
    with stocked.begin() as connection:
        for name, age in (("T-old", 20), ("T-new", 3)):
            connection.execute(
                sa.text(
                    "INSERT INTO challenger_scores (transaction_id, model_version, risk_score, "
                    "risk_band, component_scores, latency_ms, assessed_at) "
                    "VALUES (:id, 'risk-x', 1.0, 'Low', '{}', 2.0, :at)"
                ),
                {"id": name, "at": NOW - age * DAY},
            )

    purged = purge(stocked, SimulationClock(stocked), retention(challenger_scores=10))

    assert purged is not None and purged.deleted["challenger_scores"] == 1
    with stocked.connect() as connection:
        left = connection.execute(sa.text("SELECT transaction_id FROM challenger_scores")).all()
    assert [row.transaction_id for row in left] == ["T-new"]


def test_purge_keeps_a_row_exactly_one_retention_period_old(stocked: sa.Engine):
    purge(
        stocked,
        SimulationClock(stocked),
        retention(transactions=10, risk_assessments=10, alerts=10),
    )

    with stocked.connect() as connection:
        oldest = connection.execute(
            sa.text("SELECT min(occurred_at) FROM transactions")
        ).scalar_one()
    assert oldest == NOW - 10 * DAY


def test_purge_ages_data_by_the_simulation_clock_not_the_wall_clock(stocked: sa.Engine):
    """Data dated 2030 is far in the future of the wall clock, and is still purged once the
    simulation clock is a year past it."""
    SimulationClock(stocked).advance(NOW + timedelta(days=400))

    purged = purge(stocked, SimulationClock(stocked), retention())

    assert purged is not None
    assert _count(stocked, "transactions") == 0


def test_purge_before_the_clock_starts_deletes_nothing(database: str):
    engine = create_engine(database)
    _wallets(engine)
    _stored(engine, NOW)

    assert purge(engine, SimulationClock(engine), retention()) is None
    assert _count(engine, "transactions") == 1


def test_purging_a_transaction_never_leaves_an_assessment_or_alert_behind(stocked: sa.Engine):
    purge(
        stocked,
        SimulationClock(stocked),
        retention(transactions=12, risk_assessments=12, alerts=12),
    )

    with stocked.connect() as connection:
        orphans = connection.execute(
            sa.text(
                "SELECT count(*) FROM risk_assessments a"
                " LEFT JOIN transactions t USING (transaction_id) WHERE t.transaction_id IS NULL"
            )
        ).scalar_one()
    assert orphans == 0


def test_the_shipped_retention_keeps_the_audit_log_longest():
    days = load_retention_config(RETENTION_CONFIG).days

    others = [
        days.transactions,
        days.risk_assessments,
        days.alerts,
        days.sim_swaps,
        days.fraud_labels,
    ]
    assert days.audit_log > max(others)


def test_retention_config_refuses_an_audit_log_kept_no_longer_than_other_data():
    with pytest.raises(ValidationError, match="audit_log"):
        retention(audit_log=365)


def test_retention_config_refuses_an_assessment_kept_longer_than_its_transaction():
    with pytest.raises(ValidationError, match="no longer than"):
        retention(transactions=30, risk_assessments=60)


def test_txrisk_purge_reports_what_it_deleted(
    stocked: sa.Engine, database: str, capsys: pytest.CaptureFixture[str]
):
    exit_code = main(["purge", "--database-url", database, "--config", str(RETENTION_CONFIG)])

    assert exit_code == 0
    assert "0 transactions" in capsys.readouterr().out
