"""Degraded mode: with the Champion model missing, failing or slow, Transactions are scored from
Rule hits alone, marked degraded, and re-scored by the full model once it is healthy."""

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from api_helpers import ROOT, as_app_role, body, issue_key, settings, with_key
from degraded_helpers import Monotonic, StubBundle, drain, on_app, populate
from fastapi.testclient import TestClient
from market_helpers import Market, build_market, train
from scoring_helpers import HOUR, NOW, SUBJECT, tx

from txrisk.api import create_app
from txrisk.cli import main
from txrisk.features import Transaction
from txrisk.generator import GeneratorConfig
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import (
    RULES_ONLY,
    Champion,
    FixedClock,
    InMemoryMetrics,
    RiskBand,
    ScoringService,
)
from txrisk.scoring.metrics import DEGRADED_ASSESSMENTS, RESCORED
from txrisk.store import PostgresHistory, StoredScoring, create_engine, records

SWAP = NOW - 2 * HOUR


@pytest.fixture
def world(database: str) -> str:
    populate(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        records.add_sim_swap(connection, SUBJECT, SWAP)
    engine.dispose()
    return database


class Scoring:
    """`StoredScoring` over the world, with a stub bundle behind a Champion whose breaker clock
    is moved by hand."""

    def __init__(self, database: str, stub: StubBundle, *, timeout: float | None = None) -> None:
        api = settings(database)
        self.stub = stub
        self.clock = Monotonic()
        self.metrics = InMemoryMetrics()
        self.engine = create_engine(as_app_role(database))
        self.champion = Champion.of(
            stub, timeout=timeout, retry_after=30, metrics=self.metrics, monotonic=self.clock
        )
        self.scoring = StoredScoring(
            self.engine,
            features=api.features(),
            rules=api.rules(),
            bundle=self.champion,
            metrics=self.metrics,
        )

    def assess(self, transaction: Transaction):
        return self.scoring.assess(transaction).assessment

    def heal(self) -> None:
        """The model works again, and the breaker's pause is over."""
        self.stub.fail = False
        self.stub.delay = 0.0
        self.clock.advance(31)

    def alerts(self) -> dict[str, int]:
        with self.engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT transaction_id, alert_id FROM alerts"))
            return {r.transaction_id: r.alert_id for r in rows}


def test_a_failing_model_scores_rules_only_marked_degraded_and_a_critical_rule_still_forces_high(
    world: str,
):
    scoring = Scoring(world, StubBundle(fail=True))
    drained = drain(NOW)

    assessment = scoring.assess(drained)

    assert assessment.degraded
    assert assessment.model_version == RULES_ONLY
    assert assessment.risk_band is RiskBand.HIGH
    assert assessment.risk_score >= 90  # the rules scale: a Critical rule is at least 90
    assert assessment.reasons[0].code == "SIM_SWAP_DRAIN"
    assert set(scoring.alerts()) == {drained.transaction_id}
    assert scoring.metrics.value(DEGRADED_ASSESSMENTS, reason="error") == 1


def test_a_degraded_assessment_is_the_rules_only_one_on_the_rules_scale_and_bands(world: str):
    """The model's scale (High near 17) is not the rules' (High at 50): a degraded assessment is
    exactly what a service with no model gives, plus the flag."""
    scoring = Scoring(world, StubBundle(default=99.0, fail=True))
    plain = on_app(tx(NOW, amount=900.0, counterparty="w-stranger", balance=1000.0))
    api = settings(world)

    degraded = scoring.assess(plain)

    with scoring.engine.connect() as connection:
        offline = ScoringService(
            PostgresHistory(connection),
            FixedClock(plain.occurred_at),
            features=api.features(),
            rules=api.rules(),
        ).assess(plain)
    assert degraded == dataclasses.replace(offline, degraded=True)
    assert degraded.risk_score == round(degraded.risk_score)


def test_a_model_bundle_that_is_missing_or_corrupt_degrades_instead_of_stopping_the_api(
    world: str, tmp_path: Path
):
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / "bundle.pkl").write_bytes(b"not a pickle")
    for hours, folder in enumerate((tmp_path / "missing", corrupt)):
        app = create_app(settings(world, folder))
        with TestClient(app, headers=with_key(issue_key(world))) as client:
            response = client.post("/v1/score", json=body(drain(NOW + hours * HOUR)))

            scored = response.json()
            assert response.status_code == 200
            assert scored["degraded"] is True
            assert scored["model_version"] == RULES_ONLY
            assert scored["risk_band"] == "High"
            assert scored["reasons"][0]["code"] == "SIM_SWAP_DRAIN"
            active = client.get("/v1/models/active").json()
            assert active["model_version"] == RULES_ONLY


def test_a_slow_model_times_out_into_degraded_mode_and_is_then_left_alone_for_a_while(world: str):
    scoring = Scoring(world, StubBundle(delay=0.4), timeout=0.05)

    first = scoring.assess(on_app(tx(NOW, amount=40.0)))
    second = scoring.assess(on_app(tx(NOW + HOUR, amount=41.0)))

    assert first.degraded and second.degraded
    assert scoring.metrics.value(DEGRADED_ASSESSMENTS, reason="timeout") == 1
    assert scoring.metrics.value(DEGRADED_ASSESSMENTS, reason="unavailable") == 1
    # The breaker is open: the second Transaction did not wait for the model.
    assert scoring.stub.calls == 1
    assert scoring.scoring.model_health().degraded


def test_recovery_ends_degraded_mode_after_one_good_call(world: str):
    scoring = Scoring(world, StubBundle(fail=True))
    assert scoring.assess(on_app(tx(NOW, amount=40.0))).degraded
    assert scoring.scoring.model_health().degraded
    assert not scoring.scoring.rescore_due()

    scoring.heal()
    healthy = scoring.assess(on_app(tx(NOW + HOUR, amount=41.0)))

    assert not healthy.degraded
    assert healthy.model_version == "stub-1"
    assert not scoring.scoring.model_health().degraded
    assert scoring.scoring.rescore_due()  # once
    assert not scoring.scoring.rescore_due()


def test_rescoring_replaces_the_stored_assessment_and_keeps_the_degraded_one(world: str):
    scoring = Scoring(world, StubBundle(fail=True))
    transaction = on_app(tx(NOW, amount=40.0))
    degraded = scoring.assess(transaction)
    scoring.heal()

    result = scoring.scoring.rescore_degraded()

    assert (result.rescored, result.remaining, result.stopped) == (1, 0, None)
    replayed = scoring.scoring.assess(transaction)
    assert replayed.replayed
    assert not replayed.assessment.degraded
    assert replayed.assessment.model_version == "stub-1"
    with scoring.engine.connect() as connection:
        assert records.assessment_history(connection, transaction.transaction_id) == [degraded]
        assert records.awaiting_rescore(connection) == []
    assert scoring.metrics.value(RESCORED) == 1
    # Nothing is left to do the second time.
    assert scoring.scoring.rescore_degraded().rescored == 0


def test_rescoring_never_duplicates_an_alert_whatever_the_new_band(world: str):
    """A degraded High already has its Alert; a Low that turns High gets exactly one; a High
    that turns Low keeps its Alert (an Alert is a fact that was raised)."""
    scoring = Scoring(world, StubBundle(fail=True))
    still_high = drain(NOW)
    low_to_high = on_app(tx(NOW + HOUR, amount=40.0))
    low_to_low = on_app(tx(NOW + 2 * HOUR, amount=41.0))
    high_to_low = drain(NOW + 3 * HOUR)
    for t in (still_high, low_to_high, low_to_low, high_to_low):
        scoring.assess(t)
    before = scoring.alerts()
    assert set(before) == {still_high.transaction_id, high_to_low.transaction_id}
    scoring.stub.scores = {still_high.transaction_id: 30.0, low_to_high.transaction_id: 30.0}
    scoring.heal()

    result = scoring.scoring.rescore_degraded()
    again = scoring.scoring.rescore_degraded()

    after = scoring.alerts()
    assert (result.rescored, result.alerts_raised, result.remaining) == (4, 1, 0)
    assert again.rescored == 0
    assert set(after) == set(before) | {low_to_high.transaction_id}
    assert all(after[i] == before[i] for i in before)  # the same Alerts, not new ones
    bands = {
        t.transaction_id: scoring.scoring.assess(t).assessment.risk_band
        for t in (still_high, low_to_high, low_to_low, high_to_low)
    }
    assert bands == {
        still_high.transaction_id: RiskBand.HIGH,
        low_to_high.transaction_id: RiskBand.HIGH,
        low_to_low.transaction_id: RiskBand.LOW,
        high_to_low.transaction_id: RiskBand.LOW,
    }
    with scoring.engine.connect() as connection:
        listed = {a.transaction_id: a for a in records.list_alerts(connection, limit=10)}
    assert listed[high_to_low.transaction_id].assessment.risk_band is RiskBand.LOW


def test_rescoring_stops_while_the_model_is_still_down_and_loses_nothing(world: str):
    scoring = Scoring(world, StubBundle(fail=True))
    transactions = [on_app(tx(NOW + i * HOUR, amount=40.0 + i)) for i in range(3)]
    for t in transactions:
        scoring.assess(t)
    scoring.clock.advance(31)  # the breaker lets one try through, and it fails again

    result = scoring.scoring.rescore_degraded()

    assert result.rescored == 0
    assert result.remaining == 3
    assert result.stopped is not None and "the model broke" in result.stopped
    assert scoring.scoring.awaiting_rescore() == 3


def test_rescoring_stops_at_its_limit_and_asks_to_be_called_again(world: str):
    scoring = Scoring(world, StubBundle(fail=True))
    for i in range(3):
        scoring.assess(on_app(tx(NOW + i * HOUR, amount=40.0 + i)))
    scoring.heal()

    first = scoring.scoring.rescore_degraded(limit=2)

    assert (first.rescored, first.remaining) == (2, 1)
    assert scoring.scoring.rescore_due()
    assert scoring.scoring.rescore_degraded().remaining == 0


@pytest.fixture
def client(world: str) -> Iterator[TestClient]:
    """The API with a model bundle configured that is not there."""
    app = create_app(settings(world, Path("/nonexistent/bundle")))
    with TestClient(app, headers=with_key(issue_key(world))) as client:
        yield client


def test_readiness_stays_up_and_reports_degraded_mode_and_its_backlog(client: TestClient):
    client.post("/v1/score", json=body(on_app(tx(NOW, amount=40.0))))

    ready = client.get("/v1/ready")

    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert ready.json()["degraded"] is True
    assert "model bundle unavailable" in ready.json()["reason"]
    assert ready.json()["awaiting_rescore"] == 1


def test_readiness_is_not_degraded_without_a_configured_model(world: str):
    """Rules-only by configuration is a choice, not Degraded mode."""
    with TestClient(create_app(settings(world))) as client:
        ready = client.get("/v1/ready")

    assert ready.json() == {
        "status": "ok",
        "detail": None,
        "degraded": False,
        "reason": None,
        "awaiting_rescore": 0,
    }


def test_the_degraded_scale_is_documented_in_the_api_schema(client: TestClient):
    schema = client.get("/v1/openapi.json").json()["components"]["schemas"]["AssessmentOut"]

    assert "rules-only" in schema["properties"]["model_version"]["description"]
    assert "rules scale" in schema["properties"]["degraded"]["description"]


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def trained(market: Market, tmp_path_factory: pytest.TempPathFactory) -> tuple[ModelBundle, Path]:
    bundle = train(market)
    folder = tmp_path_factory.mktemp("models") / bundle.version
    bundle.save(folder)
    return bundle, folder


def test_the_api_recovers_when_the_bundle_appears_and_rescores_in_the_background(
    world: str, trained: tuple[ModelBundle, Path], tmp_path: Path
):
    bundle, saved = trained
    folder = tmp_path / "champion"
    app = create_app(settings(world, folder, model_retry_seconds=0, scoring_timeout_ms=60_000))
    with TestClient(app, headers=with_key(issue_key(world))) as client:
        early = drain(NOW)
        first = client.post("/v1/score", json=body(early)).json()
        assert first["degraded"] is True
        assert client.get("/v1/ready").json()["awaiting_rescore"] == 1

        folder.mkdir()
        for file in saved.iterdir():
            (folder / file.name).write_bytes(file.read_bytes())
        later = client.post("/v1/score", json=body(on_app(tx(NOW + HOUR, amount=40.0)))).json()

        assert later["degraded"] is False
        assert later["model_version"] == bundle.version
        ready = client.get("/v1/ready").json()
        assert (ready["degraded"], ready["awaiting_rescore"]) == (False, 0)
        stored = client.get(f"/v1/transactions/{early.transaction_id}").json()["assessment"]
        assert stored["degraded"] is False
        assert stored["model_version"] == bundle.version
        # Its Alert from Degraded mode is the only one: a Critical rule is High on either path.
        alerts = client.get("/v1/alerts").json()["alerts"]
        assert [a["transaction_id"] for a in alerts] == [early.transaction_id]


def test_the_rescore_command_replaces_degraded_assessments_and_fails_while_the_model_is_down(
    world: str,
    trained: tuple[ModelBundle, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    bundle, saved = trained
    monkeypatch.setenv("TXRISK_DATABASE_URL", world)
    monkeypatch.setenv("TXRISK_MODEL_BUNDLE", str(saved))
    monkeypatch.setenv("TXRISK_SCORING_TIMEOUT_MS", "60000")
    for name in ("generator", "features", "rules"):
        monkeypatch.setenv(f"TXRISK_{name.upper()}_CONFIG", str(ROOT / "config" / f"{name}.toml"))
    degraded = drain(NOW)
    down = StoredScoring(
        create_engine(world),
        features=settings(world).features(),
        rules=settings(world).rules(),
        bundle=Champion(_unavailable),
    )
    assert down.assess(degraded).assessment.degraded

    monkeypatch.setenv("TXRISK_MODEL_BUNDLE", str(tmp_path / "missing"))
    assert main(["rescore"]) == 3
    monkeypatch.setenv("TXRISK_MODEL_BUNDLE", str(saved))
    assert main(["rescore"]) == 0

    with create_engine(world).connect() as connection:
        stored = records.stored_assessment(connection, degraded.transaction_id)
        assert stored is not None
        assert (stored.degraded, stored.model_version) == (False, bundle.version)
        assert len(records.assessment_history(connection, degraded.transaction_id)) == 1


def _unavailable() -> ModelBundle:
    raise FileNotFoundError("no bundle")


def test_the_application_role_may_only_replace_degraded_assessments(world: str):
    scoring = Scoring(world, StubBundle())
    healthy = scoring.assess(on_app(tx(NOW, amount=40.0)))
    assert not healthy.degraded

    with scoring.engine.begin() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="only a degraded assessment"):
            connection.execute(sa.text("UPDATE risk_assessments SET risk_score = 99"))
    with scoring.engine.begin() as connection:
        with pytest.raises(sa.exc.DBAPIError, match="permission denied"):
            connection.execute(sa.text("DELETE FROM risk_assessments"))
