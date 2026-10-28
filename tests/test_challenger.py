"""Challenger shadow scoring: a Challenger model scores every Transaction beside the Champion, its
scores kept apart and never raising an Alert, and a new Challenger is picked up without a
restart."""

from pathlib import Path

import pytest
import sqlalchemy as sa
from api_helpers import as_app_role, settings
from degraded_helpers import HIGH, StubBundle, on_app, populate
from scoring_helpers import HOUR, NOW, SUBJECT, tx

from txrisk.features import Transaction
from txrisk.scoring import InMemoryMetrics, RiskBand
from txrisk.scoring.metrics import CHALLENGER_SCORES
from txrisk.store import ChallengerModel, StoredScoring, create_engine, records
from txrisk.store.roles import APP_ROLE


@pytest.fixture
def world(database: str) -> str:
    populate(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        records.add_sim_swap(connection, SUBJECT, NOW - 2 * HOUR)
    engine.dispose()
    return database


def name_challenger(database: str, version: str | None) -> None:
    """What `txrisk retrain` does to make `version` the Challenger model."""
    engine = create_engine(database)
    with engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE challenger_model SET bundle_version = :v"), {"v": version}
        )
    engine.dispose()


class Shadowed:
    """`StoredScoring` over the world, as the application role, with stub bundles for the
    Champion and, as it is named in Postgres, the Challenger."""

    def __init__(self, database: str, champion: StubBundle) -> None:
        api = settings(database)
        self.database = database
        self.bundles: dict[str, StubBundle] = {}
        self.metrics = InMemoryMetrics()
        self.engine = create_engine(as_app_role(database))
        self.challenger = ChallengerModel(
            self.engine,
            Path("models"),
            refresh_seconds=0,
            load=lambda folder: self.bundles[folder.name],  # type: ignore[arg-type, return-value]
        )
        self.scoring = StoredScoring(
            self.engine,
            features=api.features(),
            rules=api.rules(),
            bundle=champion,  # type: ignore[arg-type]
            metrics=self.metrics,
            challenger=self.challenger,
        )

    def register(self, version: str, **options: object) -> StubBundle:
        """A Challenger bundle, named as the Challenger model."""
        stub = StubBundle(**options)  # type: ignore[arg-type]
        stub.version = version
        self.bundles[version] = stub
        name_challenger(self.database, version)
        return stub

    def assess(self, transaction: Transaction):
        return self.scoring.assess(transaction)

    def shadow_scores(self) -> list[sa.Row]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.text("SELECT * FROM challenger_scores ORDER BY assessed_at, model_version")
                )
            )

    def count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one())


def champion(score: float = 1.0) -> StubBundle:
    stub = StubBundle(score)
    stub.version = "champion-1"
    return stub


def test_a_challenger_scores_every_transaction_and_its_scores_are_stored_apart(world: str):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("challenger-1", default=40.0)
    transaction = on_app(tx(NOW, amount=60.0))

    scored = shadowed.assess(transaction)

    # The Champion's assessment is what the caller gets and what is stored as the assessment.
    assert scored.assessment.model_version == "champion-1"
    assert scored.assessment.risk_score == 1.0
    assert scored.assessment.risk_band is RiskBand.LOW
    (row,) = shadowed.shadow_scores()
    assert (row.transaction_id, row.model_version) == (transaction.transaction_id, "challenger-1")
    assert row.risk_score == 40.0
    assert row.risk_band == "High"
    assert row.error is None
    assert set(row.component_scores) == {"rules", "anomaly", "supervised"}
    assert row.latency_ms >= 0
    assert row.assessed_at == transaction.occurred_at
    assert shadowed.count("risk_assessments") == 1


def test_a_challenger_never_raises_an_alert_though_it_scores_high(world: str):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("challenger-1", default=90.0)

    for transaction in (on_app(tx(NOW)), on_app(tx(NOW + HOUR, amount=70.0))):
        shadowed.assess(transaction)

    assert [r.risk_band for r in shadowed.shadow_scores()] == ["High", "High"]
    assert shadowed.count("alerts") == 0
    assert shadowed.count("cases") == 0


def test_the_champions_high_band_still_raises_its_alert_with_a_challenger_present(world: str):
    shadowed = Shadowed(world, champion(HIGH + 1))
    shadowed.register("challenger-1", default=1.0)

    scored = shadowed.assess(on_app(tx(NOW)))

    assert scored.assessment.risk_band is RiskBand.HIGH
    assert shadowed.count("alerts") == 1
    assert [r.risk_band for r in shadowed.shadow_scores()] == ["Low"]


def test_a_failing_challenger_is_recorded_and_the_champions_assessment_is_unaffected(world: str):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("challenger-1", fail=True)

    scored = shadowed.assess(on_app(tx(NOW)))

    assert not scored.assessment.degraded
    assert scored.assessment.model_version == "champion-1"
    (row,) = shadowed.shadow_scores()
    assert row.risk_score is None and row.risk_band is None
    assert row.error == "RuntimeError: the model broke"
    assert shadowed.metrics.value(CHALLENGER_SCORES, outcome="error") == 1


def test_a_challenger_that_is_the_champion_is_not_scored_twice(world: str):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("champion-1", default=50.0)

    shadowed.assess(on_app(tx(NOW)))

    assert shadowed.shadow_scores() == []


def test_a_replayed_transaction_is_not_shadow_scored_again(world: str):
    shadowed = Shadowed(world, champion(1.0))
    challenger = shadowed.register("challenger-1")
    transaction = on_app(tx(NOW))

    shadowed.assess(transaction)
    calls = challenger.calls
    replay = shadowed.assess(transaction)

    assert replay.replayed
    assert challenger.calls == calls
    assert len(shadowed.shadow_scores()) == 1


def test_a_new_challenger_is_picked_up_without_a_restart(world: str):
    shadowed = Shadowed(world, champion(1.0))

    shadowed.assess(on_app(tx(NOW)))  # no Challenger yet
    shadowed.register("challenger-1")
    shadowed.assess(on_app(tx(NOW + HOUR)))
    shadowed.register("challenger-2")  # retraining registered another, while the process runs
    shadowed.assess(on_app(tx(NOW + 2 * HOUR)))

    assert [r.model_version for r in shadowed.shadow_scores()] == ["challenger-1", "challenger-2"]


def test_withdrawing_the_challenger_stops_shadow_scoring(world: str):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("challenger-1")
    shadowed.assess(on_app(tx(NOW)))

    name_challenger(world, None)
    shadowed.assess(on_app(tx(NOW + HOUR)))

    assert len(shadowed.shadow_scores()) == 1


def test_a_challenger_bundle_that_will_not_load_does_not_stop_scoring(world: str):
    shadowed = Shadowed(world, champion(1.0))
    name_challenger(world, "never-saved")  # named, but its bundle is not in the models folder

    scored = shadowed.assess(on_app(tx(NOW)))

    assert scored.assessment.model_version == "champion-1"
    assert shadowed.shadow_scores() == []
    # Once its bundle is there, the next poll loads it.
    shadowed.register("never-saved")
    shadowed.assess(on_app(tx(NOW + HOUR)))
    assert len(shadowed.shadow_scores()) == 1


def test_scoring_without_a_champion_model_still_scores_a_challenger_in_shadow(world: str):
    api = settings(world)
    engine = create_engine(as_app_role(world))
    stub = StubBundle(default=30.0)
    stub.version = "challenger-1"
    name_challenger(world, "challenger-1")
    scoring = StoredScoring(
        engine,
        features=api.features(),
        rules=api.rules(),
        challenger=ChallengerModel(
            engine,
            Path("models"),
            refresh_seconds=0,
            load=lambda _: stub,  # type: ignore[arg-type, return-value]
        ),
    )

    scored = scoring.assess(on_app(tx(NOW)))

    assert scored.assessment.model_version == "rules-only"
    with engine.connect() as connection:
        rows = connection.execute(sa.text("SELECT risk_band FROM challenger_scores")).all()
    assert [r.risk_band for r in rows] == ["High"]


def test_the_application_role_may_store_challenger_scores_but_not_change_or_delete_them(
    world: str,
):
    shadowed = Shadowed(world, champion(1.0))
    shadowed.register("challenger-1")
    shadowed.assess(on_app(tx(NOW)))

    with shadowed.engine.connect() as connection:
        for statement in (
            "UPDATE challenger_scores SET risk_score = 0",
            "DELETE FROM challenger_scores",
            "UPDATE challenger_model SET bundle_version = 'x'",
            "INSERT INTO retraining_runs (trigger, cutoff, bundle_version, transactions, "
            "fraud_labels, reason) VALUES ('weekly', '2030-01-01', 'x', 0, 0, 'x')",
        ):
            with pytest.raises(sa.exc.ProgrammingError, match="permission denied"):
                connection.execute(sa.text(statement))
            connection.rollback()
    assert sa.make_url(shadowed.engine.url).username == APP_ROLE


def test_the_challenger_is_found_in_the_models_folder_beside_the_champions_bundle():
    api = settings("postgresql://unused/unused", bundle=Path("/models/risk-abc"))
    engine = sa.create_engine("postgresql+psycopg://unused/unused")

    found = api.challenger(engine)

    assert found is not None
    assert settings("postgresql://unused/unused").challenger(engine) is None


def test_the_challenger_poll_waits_between_reads_of_postgres(world: str):
    engine = create_engine(as_app_role(world))
    reads: list[float] = []
    now = [0.0]
    challenger = ChallengerModel(
        engine,
        Path("models"),
        refresh_seconds=5,
        monotonic=lambda: now[0],
        load=lambda _: None,  # type: ignore[arg-type, return-value]
    )
    original = engine.connect

    def counting_connect(*args: object, **kwargs: object):
        reads.append(now[0])
        return original(*args, **kwargs)

    engine.connect = counting_connect  # type: ignore[method-assign]

    for second in (0, 1, 2, 4, 5, 6):
        now[0] = float(second)
        challenger.current()

    assert reads == [0.0, 5.0]
