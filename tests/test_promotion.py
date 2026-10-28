"""Promotion gates, manual Promotion, rollback and the model card API: a Challenger model becomes
the Champion model only when an Admin asks for it and both gates pass, checked by the server; one
call puts the previous Champion model back; every action is audited. Against real Postgres, with
bundles whose scores the test scripts."""

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import sqlalchemy as sa
from api_helpers import WallClock, as_app_role, issue_key, settings
from api_helpers import signed_in as bearer
from api_helpers import with_key as service
from degraded_helpers import StubBundle, on_app, populate
from fastapi.testclient import TestClient
from httpx import Response
from idp_helpers import LocalIdentityProvider
from market_helpers import Market, build_market, train
from scoring_helpers import HOUR, NOW, RULES_CONFIG, tx

from txrisk.api import create_app
from txrisk.evaluation import load_evaluation_config
from txrisk.evaluation.thresholds import BandThresholds
from txrisk.generator import GeneratorConfig
from txrisk.models.bundle import ModelBundle
from txrisk.models.inputs import INPUT_COLUMNS
from txrisk.models.stacked import Stacker
from txrisk.promotion import backtest_gate, load_promotion_config, matured_window
from txrisk.scoring import InMemoryMetrics, RiskAssessment, RiskBand, load_rules_config
from txrisk.store import (
    FraudLabels,
    SimulationClock,
    StoredScoring,
    create_engine,
    records,
)
from txrisk.store import promotions as store
from txrisk.store.champion import FollowedChampion

DAY = timedelta(days=1)
# The simulated time the gates are checked at. With a 3-day Label maturity window and a 5-day
# backtest window, the matured window is the five days ending three days before it.
AS_OF = datetime(2030, 6, 20, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2030, 6, 12, 12, 0, tzinfo=UTC)


# --- bundles whose Risk scores the test decides ----------------------------------------------


@dataclass(frozen=True)
class ScriptedBundle(ModelBundle):
    """A model bundle that scores each Transaction as `script` says (else 0), so a test decides
    which of two models is better. Everything else about it is a real bundle's: it is saved,
    pickled, loaded and carded as one."""

    script: dict[str, float] = field(default_factory=dict)

    def risk_scores(
        self, inputs: pd.DataFrame, rules: pd.DataFrame, components: pd.DataFrame | None = None
    ) -> pd.Series:
        return pd.Series([self.script.get(i, 0.0) for i in inputs.index], index=inputs.index)


HIGH = 5.0


def scripted(version: str, script: dict[str, float]) -> ScriptedBundle:
    return ScriptedBundle(
        version=version,
        anomaly=None,  # type: ignore[arg-type]
        supervised=None,  # type: ignore[arg-type]
        stacker=Stacker({"rules": 1.0, "anomaly": 1.0, "supervised_log_odds": 1.0}, 0.0),
        thresholds=BandThresholds(medium=1.0, high=HIGH),
        input_columns=list(INPUT_COLUMNS),
        fitted_on={},
        script=script,
    )


# --- a hand-built backtest window ------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Forty Transactions on five days, eight a day: the first of each day is fraud (known by
    the Fraud labels), in Ashanti; of the others, those at odd positions are legitimate in
    Ashanti and those at even positions legitimate in Greater Accra."""

    fraud: list[str]
    ashanti: list[str]
    accra: list[str]

    @property
    def everything(self) -> list[str]:
        return [*self.fraud, *self.ashanti, *self.accra]


def build_window(database: str) -> Window:
    populate(database)
    engine = create_engine(database)
    fraud: list[str] = []
    ashanti: list[str] = []
    accra: list[str] = []
    zero_inputs = {column: 0.0 for column in INPUT_COLUMNS}
    with engine.begin() as connection:
        for day in range(5):
            for position in range(8):
                region = "Ashanti" if position % 2 == 1 or position == 0 else "Greater Accra"
                transaction = on_app(
                    tx(WINDOW_START + day * DAY + position * HOUR, region=region, amount=20.0)
                )
                records.record_transaction(connection, transaction)
                assessment = RiskAssessment(
                    transaction_id=transaction.transaction_id,
                    risk_score=1.0,
                    risk_band=RiskBand.LOW,
                    reasons=(),
                    component_scores={"rules": 0.0, "anomaly": 0.1, "supervised": 0.1},
                    model_version="champion-1",
                    model_inputs=zero_inputs,
                )
                records.save_assessment(connection, assessment, assessed_at=transaction.occurred_at)
                records.save_model_inputs(
                    connection, assessment, assessed_at=transaction.occurred_at
                )
                bucket = fraud if position == 0 else ashanti if region == "Ashanti" else accra
                bucket.append(transaction.transaction_id)
    labels = FraudLabels(engine)
    for transaction_id in fraud:
        labels.record(transaction_id, AS_OF - DAY, "complaint")
    SimulationClock(engine).advance(AS_OF)
    engine.dispose()
    return Window(fraud, ashanti, accra)


def champion_script(window: Window) -> dict[str, float]:
    """The Champion: ranks three of the five frauds High but flags one legitimate Ashanti
    Transaction too, and ranks the other two frauds below a legitimate one."""
    return {
        **{t: 6.0 for t in window.fraud[:3]},
        **{t: 3.0 for t in window.fraud[3:]},
        window.ashanti[0]: 6.0,
        window.ashanti[1]: 4.0,
    }


def better_script(window: Window) -> dict[str, float]:
    """A Challenger that ranks every fraud above everything else and flags the same one
    legitimate Transaction High (so no false positive rate rises)."""
    return {**{t: 9.5 for t in window.fraud}, window.ashanti[0]: 9.0, window.ashanti[1]: 4.0}


def worse_script(window: Window) -> dict[str, float]:
    """A Challenger that ranks the frauds below legitimate Transactions."""
    return {**{t: 1.0 for t in window.fraud}, **{t: 9.0 for t in window.ashanti[:6]}}


def shifted_script(window: Window) -> dict[str, float]:
    """A better ranking whose one legitimate High Transaction is in Greater Accra, where the
    Champion flagged none: the same overall rate, but a rise in that segment."""
    return {**{t: 9.5 for t in window.fraud}, window.accra[0]: 9.0}


# --- the shadow gate's data ------------------------------------------------------------------


def shadow(
    database: str,
    version: str,
    *,
    days: int = 9,
    high_per_day: int = 2,
    errors: int = 0,
    latency_ms: float = 5.0,
) -> None:
    """What the Challenger scored in shadow: five Transactions a day for `days` days, the first
    `high_per_day` of each High, then `errors` failed scorings."""
    engine = create_engine(database)
    start = AS_OF - days * DAY
    rows: list[dict[str, Any]] = []
    for day in range(days):
        for i in range(5):
            rows.append(
                {
                    "transaction_id": f"{version}-{day}-{i}",
                    "model_version": version,
                    "risk_score": 9.0 if i < high_per_day else 1.0,
                    "risk_band": "High" if i < high_per_day else "Low",
                    "component_scores": '{"rules": 0.0}',
                    "error": None,
                    "latency_ms": latency_ms,
                    "assessed_at": start + day * DAY + i * HOUR,
                }
            )
    for i in range(errors):
        rows.append(
            {
                "transaction_id": f"{version}-failed-{i}",
                "model_version": version,
                "risk_score": None,
                "risk_band": None,
                "component_scores": None,
                "error": "RuntimeError: the model broke",
                "latency_ms": latency_ms,
                "assessed_at": AS_OF - HOUR,
            }
        )
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO challenger_scores (transaction_id, model_version, risk_score, "
                "risk_band, component_scores, error, latency_ms, assessed_at) VALUES "
                "(:transaction_id, :model_version, :risk_score, :risk_band, "
                "CAST(:component_scores AS jsonb), :error, :latency_ms, :assessed_at)"
            ),
            rows,
        )
    engine.dispose()


# --- the API over it -------------------------------------------------------------------------


EVALUATION = (
    "label_maturity_days = 3\nalert_budget_per_day = 2\nwallet_age_band_edges = [30, 180]\n"
    "[splits]\ntrain = [0, 8]\ncalibration = [8, 11]\nthresholds = [11, 14]\n"
    "evaluation = [14, 21]\n"
)
PROMOTION = (
    "[backtest]\nwindow_days = 5\nmin_positives = 3\nmax_segment_fpr_rise = 0.2\n"
    "min_segment_negatives = 5\n"
    "[shadow]\nmin_days = 7\nvolume_tolerance = 0.25\np95_latency_ms = 200\n"
)


class Operations:
    """The API called by an Admin, a Supervisor, an Analyst and a service, over a world with a
    Champion model, `champion-1`, and a Challenger model, `challenger-1`, whose scripts the test
    chooses."""

    def __init__(self, database: str, idp: LocalIdentityProvider, folder: Path) -> None:
        self.database = database
        self.folder = folder
        self.window = build_window(database)
        self.models = folder / "models"
        self.models.mkdir()
        (folder / "evaluation.toml").write_text(EVALUATION, encoding="utf-8")
        (folder / "promotion.toml").write_text(PROMOTION, encoding="utf-8")
        self.admin = bearer(idp.access("admin", preferred_username="kofi", sub="sub-kofi"))
        self.supervisor = bearer(idp.access("supervisor", preferred_username="esi"))
        self.analyst = bearer(idp.access("analyst", preferred_username="ama"))
        self.service = service(issue_key(database))
        self.idp = idp
        self.engine = create_engine(database)

    def save(self, version: str, script: dict[str, float]) -> None:
        scripted(version, script).save(self.models / version)

    def name_challenger(self, version: str | None) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE challenger_model SET bundle_version = :v"), {"v": version}
            )

    def start(self) -> TestClient:
        app = create_app(
            settings(
                self.database,
                self.models / "champion-1",
                models_dir=self.models,
                challenger_refresh_seconds=0,
                promotion_config=self.folder / "promotion.toml",
                evaluation_config=self.folder / "evaluation.toml",
                oidc_issuer=self.idp.issuer,
                oidc_jwks_url=self.idp.jwks_url,
            ),
            wall_clock=WallClock(),
        )
        client = TestClient(app)
        client.__enter__()
        self.client = client
        return client

    def rows(self, query: str) -> list[sa.Row]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.text(query)))

    def audited(self, action: str) -> list[dict[str, Any]]:
        found = self.client.get("/v1/audit", params={"action": action}, headers=self.admin)
        assert found.status_code == 200, found.text
        entries: list[dict[str, Any]] = found.json()["entries"]
        return entries

    def champion(self) -> str:
        active = self.client.get("/v1/models/active", headers=self.analyst)
        assert active.status_code == 200
        return str(active.json()["model_version"])

    def promote(self, version: str, who: dict[str, str] | None = None) -> Response:
        return self.client.post(f"/v1/models/{version}/promote", headers=who or self.admin)

    def roll_back(self, who: dict[str, str] | None = None) -> Response:
        return self.client.post("/v1/models/rollback", headers=who or self.admin)


@pytest.fixture
def ops(database: str, local_idp: LocalIdentityProvider, tmp_path: Path) -> Iterator[Operations]:
    operations = Operations(database, local_idp, tmp_path)
    operations.save("champion-1", champion_script(operations.window))
    yield operations
    operations.engine.dispose()
    operations.client.__exit__(None, None, None)


def ready(ops: Operations, script: dict[str, float] | None = None, **shadow_options: Any) -> None:
    """A Challenger model that has been named, saved and scored in shadow."""
    ops.save("challenger-1", script or better_script(ops.window))
    ops.name_challenger("challenger-1")
    shadow(ops.database, "challenger-1", **shadow_options)
    ops.start()


# --- Promotion -------------------------------------------------------------------------------


def test_an_admin_promotes_a_challenger_that_passes_both_gates(ops: Operations):
    ready(ops)
    assert ops.champion() == "champion-1"

    promoted = ops.promote("challenger-1")

    assert promoted.status_code == 200, promoted.text
    body = promoted.json()
    assert (body["action"], body["from_version"], body["to_version"]) == (
        "promote",
        "champion-1",
        "challenger-1",
    )
    assert body["actor_name"] == "kofi"
    assert body["gates"]["passed"] is True
    assert ops.champion() == "challenger-1"
    models = ops.client.get("/v1/models", headers=ops.analyst).json()
    assert models["champion"] == "challenger-1"
    assert [h["to_version"] for h in models["history"]] == ["challenger-1"]


def test_the_gates_report_what_the_challenger_did_against_the_champion(ops: Operations):
    ready(ops)

    gates = ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin)

    assert gates.status_code == 200, gates.text
    report = gates.json()
    assert report["passed"] is True
    backtest = report["backtest"]
    assert backtest["transactions"] == 40
    assert backtest["positives"] == 5
    assert backtest["challenger"]["pr_auc"] == 1.0
    assert backtest["challenger"]["pr_auc"] > backtest["champion"]["pr_auc"]
    assert (
        backtest["challenger"]["precision_at_budget"] >= backtest["champion"]["precision_at_budget"]
    )
    assert backtest["failures"] == []
    shadow_report = report["shadow"]
    assert shadow_report["scored"] == 45
    assert shadow_report["errors"] == 0
    assert shadow_report["days"] >= 7
    assert 1.5 <= shadow_report["alerts_per_day"] <= 2.5
    assert shadow_report["p95_latency_ms"] == 5.0
    # Looking at the gates changes nothing.
    assert ops.champion() == "champion-1"


def test_a_challenger_that_ranks_fraud_lower_than_the_champion_is_refused(ops: Operations):
    ready(ops, worse_script(ops.window))

    refused = ops.promote("challenger-1")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert "PR-AUC" in detail["message"]
    assert detail["gates"]["backtest"]["passed"] is False
    assert detail["gates"]["shadow"]["passed"] is True
    assert ops.champion() == "champion-1"
    assert ops.rows("SELECT * FROM model_promotions") == []
    (entry,) = ops.audited("model.promote")
    assert entry["outcome"] == "failed"
    assert entry["detail"]["gates"]["backtest"]["failures"]


def test_a_challenger_whose_false_positive_rate_rises_in_one_segment_is_refused(ops: Operations):
    ready(ops, shifted_script(ops.window))

    gates = ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin).json()

    backtest = gates["backtest"]
    # It ranks better, so only the segment rule stops it.
    assert backtest["challenger"]["pr_auc"] > backtest["champion"]["pr_auc"]
    assert backtest["passed"] is False
    (failure,) = backtest["failures"]
    assert "region Greater Accra" in failure
    accra = next(s for s in backtest["segments"] if s["member"] == "Greater Accra")
    assert accra["champion_fpr"] == 0.0
    assert accra["challenger_fpr"] > 0
    assert ops.promote("challenger-1").status_code == 409
    assert ops.champion() == "champion-1"


def test_a_segment_too_small_to_judge_does_not_stop_a_promotion(ops: Operations, tmp_path: Path):
    (tmp_path / "promotion.toml").write_text(
        PROMOTION.replace("min_segment_negatives = 5", "min_segment_negatives = 100"),
        encoding="utf-8",
    )
    ready(ops, shifted_script(ops.window))

    gates = ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin).json()

    assert gates["backtest"]["passed"] is True
    accra = next(s for s in gates["backtest"]["segments"] if s["member"] == "Greater Accra")
    assert accra["judged"] is False


def test_too_few_known_frauds_in_the_window_fails_the_backtest_rather_than_passing_on_noise(
    ops: Operations, tmp_path: Path
):
    (tmp_path / "promotion.toml").write_text(
        PROMOTION.replace("min_positives = 3", "min_positives = 6"), encoding="utf-8"
    )
    ready(ops)

    gates = ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin).json()

    assert gates["backtest"]["passed"] is False
    assert "known Fraud labels" in gates["backtest"]["failures"][0]


def test_frauds_discovered_after_the_simulated_time_are_not_known_to_the_backtest(
    ops: Operations,
):
    with ops.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE fraud_labels SET discovered_at = :later"), {"later": AS_OF + DAY}
        )
    ready(ops)

    gates = ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin).json()

    assert gates["backtest"]["positives"] == 0
    assert gates["backtest"]["passed"] is False


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"days": 4}, "simulated days"),
        ({"errors": 1}, "failed to score 1"),
        ({"high_per_day": 4}, "Alert volume"),
        ({"high_per_day": 1}, "Alert volume"),
        ({"latency_ms": 500.0}, "p95 scoring latency"),
    ],
)
def test_a_challenger_that_fails_the_shadow_gate_is_refused(
    ops: Operations, options: dict[str, Any], reason: str
):
    ready(ops, **options)

    refused = ops.promote("challenger-1")

    assert refused.status_code == 409
    detail = refused.json()["detail"]
    assert reason in detail["message"]
    assert detail["gates"]["backtest"]["passed"] is True
    assert detail["gates"]["shadow"]["passed"] is False
    assert ops.champion() == "champion-1"


def test_a_challenger_with_no_shadow_scores_is_refused(ops: Operations):
    ops.save("challenger-1", better_script(ops.window))
    ops.name_challenger("challenger-1")
    ops.start()

    refused = ops.promote("challenger-1")

    assert refused.status_code == 409
    assert "no Transactions in shadow" in refused.json()["detail"]["message"]


def test_only_the_challenger_model_can_be_promoted(ops: Operations):
    ready(ops)
    ops.save("other-1", better_script(ops.window))

    unnamed = ops.promote("other-1")
    champion = ops.promote("champion-1")

    assert unnamed.status_code == 409
    assert "not the Challenger model" in unnamed.json()["detail"]["message"]
    assert champion.status_code == 409
    assert ops.champion() == "champion-1"


def test_the_gates_are_rechecked_at_every_promotion_so_a_stale_pass_does_not_count(
    ops: Operations,
):
    ready(ops)
    assert ops.client.get("/v1/models/challenger-1/gates", headers=ops.admin).json()["passed"]
    # The Challenger then fails scoring in shadow.
    shadow(ops.database, "challenger-1", errors=2, days=0)

    refused = ops.promote("challenger-1")

    assert refused.status_code == 409
    assert ops.champion() == "champion-1"


# --- who may promote -------------------------------------------------------------------------


def test_only_an_admin_can_promote_roll_back_or_run_the_gates_and_a_denial_is_audited(
    ops: Operations,
):
    ready(ops)
    key = ops.service

    for who in (ops.supervisor, ops.analyst, key):
        assert ops.promote("challenger-1", who).status_code in (401, 403)
        assert ops.roll_back(who).status_code in (401, 403)
        gates = ops.client.get("/v1/models/challenger-1/gates", headers=who)
        assert gates.status_code in (401, 403)

    # The gates pass, and still nothing happened.
    assert ops.champion() == "champion-1"
    assert ops.rows("SELECT * FROM model_promotions") == []
    denied = [e for e in ops.audited("model.promote") if e["outcome"] == "denied"]
    assert {e["actor_roles"][0] for e in denied if e["actor_type"] == "person"} == {
        "supervisor",
        "analyst",
    }
    assert any(e["actor_type"] == "service" for e in denied)
    assert all(e["object_id"] == "challenger-1" for e in denied)


def test_promoting_without_a_credential_is_refused(ops: Operations):
    ready(ops)

    assert ops.client.post("/v1/models/challenger-1/promote").status_code == 401


# --- audit -----------------------------------------------------------------------------------


def test_a_promotion_and_a_rollback_are_audited_with_who_and_what(ops: Operations):
    ready(ops)

    ops.promote("challenger-1")
    ops.roll_back()

    (promoted,) = ops.audited("model.promote")
    assert promoted["outcome"] == "success"
    assert promoted["actor_name"] == "kofi"
    assert promoted["object_id"] == "challenger-1"
    assert promoted["detail"]["from_version"] == "champion-1"
    assert promoted["detail"]["gates"]["passed"] is True
    (rolled_back,) = ops.audited("model.rollback")
    assert rolled_back["outcome"] == "success"
    assert rolled_back["detail"] == {"from_version": "challenger-1", "to_version": "champion-1"}


# --- rollback --------------------------------------------------------------------------------


def test_rollback_restores_the_previous_champion_in_one_call(ops: Operations):
    ready(ops)
    ops.promote("challenger-1")
    assert ops.champion() == "challenger-1"

    rolled_back = ops.roll_back()

    assert rolled_back.status_code == 200, rolled_back.text
    body = rolled_back.json()
    assert (body["action"], body["from_version"], body["to_version"]) == (
        "rollback",
        "challenger-1",
        "champion-1",
    )
    assert ops.champion() == "champion-1"
    assert ops.client.get("/v1/models", headers=ops.analyst).json()["champion"] == "champion-1"
    # The rolled-back model is still the Challenger, scoring in shadow.
    assert ops.client.get("/v1/models", headers=ops.analyst).json()["challenger"] == "challenger-1"


def test_rollback_is_one_step_and_needs_a_promotion_to_undo(ops: Operations):
    ready(ops)

    nothing = ops.roll_back()
    ops.promote("challenger-1")
    first = ops.roll_back()
    again = ops.roll_back()

    assert nothing.status_code == 409
    assert first.status_code == 200
    assert again.status_code == 409
    assert "already a rollback" in again.json()["detail"]["message"]
    assert ops.champion() == "champion-1"
    failed = [e for e in ops.audited("model.rollback") if e["outcome"] == "failed"]
    assert len(failed) == 2


def test_a_rolled_back_challenger_can_be_promoted_again_and_rolled_back_again(ops: Operations):
    ready(ops)

    ops.promote("challenger-1")
    ops.roll_back()
    second = ops.promote("challenger-1")
    back = ops.roll_back()

    assert second.status_code == 200
    assert back.status_code == 200
    assert ops.champion() == "champion-1"
    history = ops.client.get("/v1/models", headers=ops.analyst).json()["history"]
    assert [h["action"] for h in history] == ["rollback", "promote", "rollback", "promote"]


def test_after_a_second_promotion_rollback_restores_the_champion_before_it(ops: Operations):
    ready(ops)
    ops.promote("challenger-1")
    ops.save("challenger-2", better_script(ops.window))
    ops.name_challenger("challenger-2")
    shadow(ops.database, "challenger-2")

    second = ops.promote("challenger-2")
    back = ops.roll_back()

    assert second.status_code == 200, second.text
    assert second.json()["from_version"] == "challenger-1"
    assert back.json()["to_version"] == "challenger-1"
    assert ops.champion() == "challenger-1"


# --- the model card --------------------------------------------------------------------------


def test_every_role_and_a_service_with_the_scope_can_read_a_models_card(ops: Operations):
    ready(ops)
    reader = service(issue_key(ops.database, "models:read"))

    for who in (ops.analyst, ops.supervisor, ops.admin, reader):
        found = ops.client.get("/v1/models/challenger-1/card", headers=who)
        assert found.status_code == 200, found.text
        card = found.json()
        assert card["version"] == "challenger-1"
        assert {"intended_use", "data", "metrics", "limitations", "model"} <= set(card["card"])
        assert "## Intended use" in card["markdown"]
        assert "## Limitations" in card["markdown"]
    champion = ops.client.get("/v1/models/champion-1/card", headers=ops.analyst)
    assert champion.status_code == 200


def test_the_card_of_an_unknown_version_is_not_found_and_a_path_is_not_a_version(
    ops: Operations,
):
    ready(ops)

    unknown = ops.client.get("/v1/models/risk-nothing/card", headers=ops.analyst)
    traversal = ops.client.get("/v1/models/..%2F..%2Fetc/card", headers=ops.analyst)

    assert unknown.status_code == 404
    assert traversal.status_code in (404, 422)


def test_the_models_list_names_the_champion_the_challenger_and_each_retraining_run(
    ops: Operations,
):
    ready(ops)
    with ops.engine.begin() as connection:
        run = connection.execute(
            sa.text(
                "INSERT INTO retraining_runs (trigger, cutoff, bundle_version, transactions, "
                "fraud_labels, reason) VALUES ('drift', :cutoff, 'challenger-1', 10, 2, 'test') "
                "RETURNING run_id"
            ),
            {"cutoff": AS_OF},
        ).scalar_one()
        connection.execute(sa.text("UPDATE challenger_model SET run_id = :r"), {"r": run})

    models = ops.client.get("/v1/models", headers=ops.analyst).json()

    assert models["champion"] == "champion-1"
    assert models["challenger"] == "challenger-1"
    by_version = {m["version"]: m for m in models["models"]}
    assert by_version["champion-1"]["roles"] == ["champion"]
    assert by_version["challenger-1"]["roles"] == ["challenger"]
    assert by_version["challenger-1"]["trigger"] == "drift"
    assert by_version["champion-1"]["trigger"] is None


# --- the scoring path follows ----------------------------------------------------------------


def stub(version: str, score: float) -> StubBundle:
    bundle = StubBundle(score)
    bundle.version = version
    return bundle


def test_scoring_switches_champion_on_promotion_and_back_on_rollback_without_a_restart(
    database: str,
):
    populate(database)
    engine = create_engine(as_app_role(database))
    owner = create_engine(database)
    bundles = {"champion-1": stub("champion-1", 1.0), "challenger-1": stub("challenger-1", 2.0)}
    api = settings(database)
    champion = FollowedChampion(
        engine,
        configured=Path("models/champion-1"),
        models=Path("models"),
        refresh_seconds=0,
        load=lambda folder: bundles[folder.name],  # type: ignore[arg-type, return-value]
    )
    scoring = StoredScoring(
        engine,
        features=api.features(),
        rules=api.rules(),
        bundle=champion,
        metrics=InMemoryMetrics(),
    )

    def scored_by(at: datetime) -> str:
        return scoring.assess(on_app(tx(at))).assessment.model_version

    before = scored_by(NOW)
    with owner.begin() as connection:
        store.promote(
            connection,
            version="challenger-1",
            champion_version="champion-1",
            expected_pointer=None,
            actor_id="sub-kofi",
            actor_name="kofi",
            occurred_at=NOW,
            gates={},
        )
    promoted = scored_by(NOW + HOUR)
    with owner.begin() as connection:
        store.roll_back(connection, actor_id="sub-kofi", actor_name="kofi", occurred_at=NOW)
    restored = scored_by(NOW + 2 * HOUR)

    assert (before, promoted, restored) == ("champion-1", "challenger-1", "champion-1")
    engine.dispose()
    owner.dispose()


def test_a_promotion_that_raced_with_another_is_not_applied(database: str):
    owner = create_engine(database)
    with owner.begin() as connection:
        store.promote(
            connection,
            version="challenger-1",
            champion_version="champion-1",
            expected_pointer=None,
            actor_id="sub-1",
            actor_name="kofi",
            occurred_at=NOW,
            gates={},
        )

    with pytest.raises(store.ChampionChanged), owner.begin() as connection:
        store.promote(
            connection,
            version="challenger-2",
            champion_version="champion-1",
            expected_pointer=None,  # checked against the Champion before the first Promotion
            actor_id="sub-2",
            actor_name="esi",
            occurred_at=NOW,
            gates={},
        )

    with owner.connect() as connection:
        assert store.latest(connection).to_version == "challenger-1"  # type: ignore[union-attr]
    owner.dispose()


# --- real bundles ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def trained(market: Market) -> ModelBundle:
    return train(market)


def test_the_backtest_scores_the_stored_inputs_with_real_bundles_even_where_features_are_empty(
    database: str, trained: ModelBundle, tmp_path: Path
):
    window = build_window(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        # A feature that was empty when the Transaction was scored is stored as null.
        connection.execute(
            sa.text("UPDATE model_inputs SET inputs = jsonb_set(inputs, :path, 'null')"),
            {"path": [INPUT_COLUMNS[0]]},
        )
    (tmp_path / "evaluation.toml").write_text(EVALUATION, encoding="utf-8")
    (tmp_path / "promotion.toml").write_text(PROMOTION, encoding="utf-8")
    evaluation = load_evaluation_config(tmp_path / "evaluation.toml")
    config = load_promotion_config(tmp_path / "promotion.toml")
    rules = load_rules_config(RULES_CONFIG)

    matured = matured_window(
        engine, rules=rules, config=config.backtest, evaluation=evaluation, as_of=AS_OF
    )
    same = backtest_gate(
        matured,
        champion=trained,
        challenger=dataclasses.replace(trained, version="copy"),
        config=config.backtest,
        evaluation=evaluation,
    )

    assert len(matured.inputs) == 40
    assert matured.inputs[INPUT_COLUMNS[0]].isna().all()
    assert int(matured.frame["fraud"].sum()) == len(window.fraud)
    # A model matches itself: it passes, which is what "matches or beats" allows.
    assert same.passed, same.failures
    assert same.champion == same.challenger
    engine.dispose()


# --- the application's database role ---------------------------------------------------------


def test_the_application_role_can_append_promotions_but_never_change_or_delete_them(
    database: str,
):
    engine = create_engine(as_app_role(database))
    with engine.begin() as connection:
        store.promote(
            connection,
            version="challenger-1",
            champion_version="champion-1",
            expected_pointer=None,
            actor_id="sub-1",
            actor_name="kofi",
            occurred_at=NOW,
            gates={},
        )

    for statement in (
        "UPDATE model_promotions SET actor_name = 'someone else'",
        "DELETE FROM model_promotions",
        "DELETE FROM champion_model",
    ):
        with pytest.raises(sa.exc.ProgrammingError), engine.begin() as connection:
            connection.execute(sa.text(statement))
    engine.dispose()
