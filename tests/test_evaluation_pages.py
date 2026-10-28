"""The Streamlit evaluation pages: they list model versions, show a version's metrics and curves,
and compare two, with the txrisk API as their only source of data. The API is a stand-in that
answers as `/v1/models` does and records what was asked of it."""

import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from streamlit.testing.v1 import AppTest

PAGES = Path(__file__).resolve().parents[1] / "evaluation-pages"
sys.path.insert(0, str(PAGES))

import data  # noqa: E402
import shaping  # noqa: E402
from client import API_KEY_ENV, API_KEY_HEADER, Api, ApiError  # noqa: E402

SEGMENTS = ("region", "kyc_tier", "channel", "wallet_age_band")


def _report(pr_auc: float, recall: float, scenario_recall: float) -> dict[str, Any]:
    group = {
        "transactions": 100,
        "fraud": 5,
        "flagged": 4,
        "precision": 0.5,
        "recall": recall,
        "false_positive_rate": 0.01,
    }
    return {
        "scorer": "bundle",
        "days": [90, 120],
        "transactions": 1000,
        "fraud": 50,
        "fraud_discovered": 20,
        "ranking": {"roc_auc": 0.95, "pr_auc": pr_auc},
        "at_high": {
            "precision": 0.3,
            "recall": recall,
            "false_positive_rate": 0.004,
            "per_day": 99,
        },
        "at_alert_budget": {"budget_per_day": 100, "alerts": 3000, "precision": 0.27},
        "recall_by_scenario": {
            "mule_fan_in": {"fraud": 30, "recall_at_high": scenario_recall},
            "reversal_scam": {"fraud": 20, "recall_at_high": 0.5},
        },
        "recall_against": {"ground_truth": recall, "discovered_labels": 0.95},
        "segments": {name: {"all": group} for name in SEGMENTS},
        "curves": {
            "pr": {
                "x_label": "Recall",
                "y_label": "Precision",
                "x": [0, 0.5, 1],
                "y": [1, 0.7, 0.2],
            },
            "roc": {
                "x_label": "False positive rate",
                "y_label": "Recall",
                "x": [0, 0.1, 1],
                "y": [0, 0.8, 1],
            },
        },
    }


CARD = {
    "version": "v1",
    "metrics": {
        "evaluated_on": "The calibration days.",
        "overall": {"transactions": 10, "fraud": 2, "precision": 0.5, "pr_auc": 0.9},
        "by_segment": {"region": {"Accra": {"transactions": 10, "fraud": 2, "precision": 0.5}}},
    },
}

LISTED = {
    "champion": "champion-1",
    "challenger": "challenger-1",
    "models": [
        {"version": "champion-1", "roles": ["champion"], "trained_cutoff": None, "trigger": None},
        {
            "version": "challenger-1",
            "roles": ["challenger"],
            "trained_cutoff": "2026-07-01T00:00:00Z",
            "trigger": "weekly",
        },
        {"version": "old-0", "roles": [], "trained_cutoff": None, "trigger": "weekly"},
    ],
    "history": [
        {
            "promotion_id": 1,
            "action": "promote",
            "from_version": "old-0",
            "to_version": "champion-1",
            "actor_name": "Demo Admin",
            "occurred_at": "2026-07-01T00:00:00Z",
            "gates": {"passed": True},
        }
    ],
}

REPORTS = {
    "champion-1": _report(pr_auc=0.70, recall=0.80, scenario_recall=0.6),
    "challenger-1": _report(pr_auc=0.75, recall=0.85, scenario_recall=0.9),
}


class StandInApi:
    """Answers `/v1/models...` as the API does, for a key with the right scope only."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get(API_KEY_HEADER) != self.key:
            return httpx.Response(401, json={"detail": "invalid API key"})
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json=LISTED)
        version = path.split("/")[3]
        if path.endswith("/card"):
            return httpx.Response(200, json={"version": version, "card": CARD, "markdown": ""})
        if path.endswith("/evaluation") and version in REPORTS:
            return httpx.Response(200, json={"version": version, "evaluation": REPORTS[version]})
        return httpx.Response(404, json={"detail": "no evaluation report is saved"})


@pytest.fixture
def api() -> StandInApi:
    stand_in = StandInApi(secrets.token_hex(16))
    data.use(
        Api(
            httpx.Client(
                base_url="http://api.test",
                headers={API_KEY_HEADER: stand_in.key},
                transport=httpx.MockTransport(stand_in),
            )
        )
    )
    return stand_in


def _page(name: str) -> AppTest:
    app = AppTest.from_file(str(PAGES / name), default_timeout=30).run()
    assert not app.exception, app.exception
    return app


def _table_titles(app: AppTest) -> list[str]:
    return [header.value for header in app.subheader]


def test_the_front_page_lists_every_model_version_with_its_role_and_evaluation(api: StandInApi):
    app = _page("app.py")

    versions = app.dataframe[0].value
    assert list(versions["Version"]) == ["champion-1", "challenger-1", "old-0"]
    assert list(versions["Roles"]) == ["champion", "challenger", ""]
    assert list(versions["Evaluation report"]) == ["yes", "yes", "no"]
    assert [m.value for m in app.metric] == ["champion-1", "challenger-1"]
    history = app.dataframe[1].value
    assert list(history["to_version"]) == ["champion-1"]
    assert "gates" not in history.columns


def test_a_versions_page_shows_its_metrics_curves_scenarios_segments_and_the_recall_gap(
    api: StandInApi,
):
    app = _page("pages/1_Version.py")
    app.selectbox[0].select("challenger-1 (challenger)").run()

    assert not app.exception
    assert {"Metrics", "Curves", "Recall at High by Fraud scenario", "By segment"} <= set(
        _table_titles(app)
    )
    metrics = app.dataframe[0].value
    assert metrics.loc["PR-AUC", "Value"] == pytest.approx(0.75)
    gap = {m.label: m.value for m in app.metric}
    assert gap["Ground truth"] == "0.850"
    assert gap["Discovered labels"] == "0.950"
    assert gap["Fraud never discovered"] == "60.0%"
    assert len(app.tabs) == len(SEGMENTS)


def test_a_version_without_an_evaluation_report_says_how_to_make_one_and_shows_its_card(
    api: StandInApi,
):
    app = _page("pages/1_Version.py")
    app.selectbox[0].select("old-0").run()

    assert not app.exception
    assert "txrisk evaluate --scorer bundle --bundle models/old-0" in app.warning[0].value
    assert "Model card metrics" in _table_titles(app)


def test_the_compare_page_puts_the_champion_and_the_challenger_side_by_side(api: StandInApi):
    app = _page("pages/2_Compare.py")

    assert [box.value for box in app.selectbox[:2]] == [
        "champion-1 (champion)",
        "challenger-1 (challenger)",
    ]
    table = app.dataframe[0].value
    assert list(table.columns) == ["champion-1", "challenger-1", "B minus A"]
    assert table.loc["PR-AUC", "B minus A"] == pytest.approx(0.05)
    scenarios = app.dataframe[1].value
    assert scenarios.loc["mule_fan_in", "champion-1"] == pytest.approx(0.6)
    assert scenarios.loc["mule_fan_in", "challenger-1"] == pytest.approx(0.9)


def test_any_two_versions_can_be_compared_and_the_same_one_twice_is_refused(api: StandInApi):
    app = _page("pages/2_Compare.py")

    app.selectbox[0].select("challenger-1 (challenger)").run()

    assert app.info[0].value == "Pick two different versions."


def test_comparing_with_a_version_that_has_no_report_says_which(api: StandInApi):
    app = _page("pages/2_Compare.py")

    app.selectbox[1].select("old-0").run()

    assert "old-0" in app.warning[0].value


def test_the_pages_say_why_when_the_api_refuses_them():
    stand_in = StandInApi(secrets.token_hex(16))
    data.use(
        Api(
            httpx.Client(
                base_url="http://api.test",
                headers={API_KEY_HEADER: secrets.token_hex(16)},
                transport=httpx.MockTransport(stand_in),
            )
        )
    )

    app = AppTest.from_file(str(PAGES / "app.py"), default_timeout=30).run()

    assert "refused the key" in app.error[0].value


def test_everything_the_pages_read_comes_from_the_models_endpoints(api: StandInApi):
    _page("app.py")
    _page("pages/1_Version.py")
    _page("pages/2_Compare.py")

    assert {request.method for request in api.requests} == {"GET"}
    assert all(request.url.path.startswith("/v1/models") for request in api.requests)


def test_without_an_api_key_the_pages_cannot_start(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)

    with pytest.raises(ApiError, match=API_KEY_ENV):
        Api.from_environment()


def test_the_pages_have_no_way_to_reach_the_database():
    sources = "\n".join(path.read_text(encoding="utf-8") for path in PAGES.rglob("*.py"))
    dockerfile = (PAGES / "Dockerfile").read_text(encoding="utf-8")

    for forbidden in ("sqlalchemy", "psycopg", "txrisk.store", "DATABASE_URL", "PGPASSWORD"):
        assert forbidden not in sources
    assert "--only-group pages" in dockerfile


def test_curves_of_two_versions_share_one_axis():
    curves = shaping.curve_frame(
        {"a": {"curves": {"pr": {"x": [0, 1], "y": [1, 0]}}}, "b": REPORTS["champion-1"]}, "pr"
    )

    assert list(curves.columns) == ["a", "b"]
    assert list(curves.index) == [0.0, 0.5, 1.0]
    assert not curves.isna().any().any()
