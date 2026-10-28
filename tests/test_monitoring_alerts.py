"""The alerting side of monitoring: the metrics reach Prometheus, the rules and Alertmanager config
are valid, the webhook sink records what it is sent, and the dashboards query what exists."""

import json
import re
import threading
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from conftest import _require_docker
from prometheus_client import generate_latest
from test_scheduler import FakeLauncher

from txrisk.monitoring import metrics as exposition
from txrisk.monitoring.metrics import Metric, push, registry_of
from txrisk.monitoring.sink import AlertSink, serve
from txrisk.scheduler import Scheduler, load_scheduler_config
from txrisk.store import SimulationClock, create_engine

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
RULES = yaml.safe_load((DEPLOY / "prometheus" / "rules" / "monitoring.yml").read_text())
PROMETHEUS = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())
ALERTMANAGER = yaml.safe_load((DEPLOY / "alertmanager" / "alertmanager.yml").read_text())
PROMETHEUS_IMAGE = COMPOSE["prometheus"]["image"]
ALERTMANAGER_IMAGE = COMPOSE["alertmanager"]["image"]
DASHBOARDS = DEPLOY / "grafana" / "dashboards"


def rules() -> list[dict[str, Any]]:
    return [rule for group in RULES["groups"] for rule in group["rules"]]


# --- the rules and the Alertmanager config -----------------------------------------------------


def test_the_rules_cover_every_alert_the_ticket_asks_for():
    names = {rule["alert"] for rule in rules()}

    assert {
        "InputDriftAction",
        "RiskScoreDriftAction",
        "AlertVolumeOutsideBudgetBand",
        "LabelRecallRegression",
        "LabelPrecisionRegression",
    } <= names


def test_every_rule_has_a_severity_and_a_summary_and_uses_only_metrics_the_monitor_pushes():
    pushed = exposition.exposed_names()
    for rule in rules():
        assert rule["labels"]["severity"] in {"warning", "action"}, rule["alert"]
        assert rule["annotations"]["summary"], rule["alert"]
        used = set(re.findall(r"\btxrisk_monitoring_[a-z_]+", rule["expr"]))
        assert used and used <= pushed, (rule["alert"], sorted(used - pushed))


def test_the_action_rules_compare_with_the_thresholds_the_monitor_pushes_from_config():
    """No limit is written into an action rule: each is the pushed threshold, from config."""
    for rule in rules():
        if rule["labels"]["severity"] == "action":
            assert not re.search(r"[<>]=?\s*\d", rule["expr"]), rule["alert"]


def test_prometheus_loads_the_rules_and_sends_alerts_to_the_alertmanager_compose_runs():
    assert PROMETHEUS["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    [manager] = PROMETHEUS["alerting"]["alertmanagers"]
    assert manager["static_configs"][0]["targets"] == ["alertmanager:9093"]
    mounts = " ".join(COMPOSE["prometheus"]["volumes"])
    assert "./deploy/prometheus/rules:/etc/prometheus/rules" in mounts


def test_prometheus_scrapes_the_pushgateway_keeping_the_pushed_labels_and_the_sink():
    jobs = {job["job_name"]: job for job in PROMETHEUS["scrape_configs"]}

    assert jobs["pushgateway"]["honor_labels"] is True
    assert jobs["pushgateway"]["static_configs"][0]["targets"] == ["pushgateway:9091"]
    assert jobs["alert-sink"]["static_configs"][0]["targets"] == ["alert-sink:9095"]


def test_alertmanager_sends_every_alert_to_the_sink_compose_runs():
    [receiver] = ALERTMANAGER["receivers"]
    [webhook] = receiver["webhook_configs"]

    assert ALERTMANAGER["route"]["receiver"] == receiver["name"]
    assert webhook["url"] == "http://alert-sink:9095/alerts"
    assert "--port" in COMPOSE["alert-sink"]["command"]
    assert "9095" in COMPOSE["alert-sink"]["command"]
    assert webhook["send_resolved"] is True


def test_the_new_uis_are_on_this_machine_only_and_the_pushgateway_and_sink_are_not_published():
    assert all(p.startswith("127.0.0.1:") for p in COMPOSE["alertmanager"]["ports"])
    for service in ("pushgateway", "alert-sink"):
        assert "ports" not in COMPOSE[service]


def test_the_job_container_pushes_to_the_pushgateway_and_no_credential_is_committed():
    environment = COMPOSE["job"]["environment"]

    assert environment["TXRISK_PUSHGATEWAY_URL"] == "http://pushgateway:9091"
    assert "pushgateway" not in COMPOSE["job"].get("ports", [])
    for path in (
        DEPLOY / "alertmanager" / "alertmanager.yml",
        DEPLOY / "prometheus" / "prometheus.yml",
    ):
        text = path.read_text().lower()
        assert "password" not in text and "token" not in text, path


def _promtool(image: str, tool: str, *args: str) -> str:
    """Run a tool of the Prometheus images over deploy/, and return its output; raises with it if
    the tool fails."""
    import docker

    _require_docker()
    client = docker.from_env()
    try:
        client.images.get(image)
    except docker.errors.ImageNotFound:
        client.images.pull(image)
    output: bytes = client.containers.run(
        image,
        list(args),
        entrypoint=tool,
        volumes={str(DEPLOY): {"bind": "/deploy", "mode": "ro"}},
        remove=True,
        stderr=True,
    )
    return output.decode()


def test_promtool_accepts_the_rules():
    assert "SUCCESS" in _promtool(
        PROMETHEUS_IMAGE, "promtool", "check", "rules", "/deploy/prometheus/rules/monitoring.yml"
    )


def test_promtool_runs_the_rule_unit_tests_which_fire_at_the_action_thresholds():
    """PSI at or above 0.25, Alert volume outside the band and a relative drop beyond the limit
    fire; values below them do not."""
    output = _promtool(
        PROMETHEUS_IMAGE,
        "promtool",
        "test",
        "rules",
        "/deploy/prometheus/tests/monitoring_rules_test.yml",
    )

    assert "SUCCESS" in output


def test_promtool_accepts_the_prometheus_config_with_its_rules_in_place():
    """The config loads its rule files and parses; the targets need not exist."""
    # The rule glob is /etc/prometheus/rules in the container, so check the file as mounted there.
    import docker

    _require_docker()
    client = docker.from_env()
    output: bytes = client.containers.run(
        PROMETHEUS_IMAGE,
        ["check", "config", "/etc/prometheus/prometheus.yml"],
        entrypoint="promtool",
        volumes={
            str(DEPLOY / "prometheus" / "prometheus.yml"): {
                "bind": "/etc/prometheus/prometheus.yml",
                "mode": "ro",
            },
            str(DEPLOY / "prometheus" / "rules"): {"bind": "/etc/prometheus/rules", "mode": "ro"},
        },
        remove=True,
        stderr=True,
    )
    assert b"SUCCESS" in output


def test_amtool_accepts_the_alertmanager_config():
    output = _promtool(
        ALERTMANAGER_IMAGE, "amtool", "check-config", "/deploy/alertmanager/alertmanager.yml"
    )

    assert "SUCCESS" in output


# --- the metrics the monitor pushes ------------------------------------------------------------


def test_a_metric_becomes_a_gauge_with_the_labels_its_spec_names():
    text = generate_latest(
        registry_of(
            [
                Metric("psi", 0.31, member="seconds_since_sim_swap"),
                Metric("label_recall", 0.5, "channel", "ussd"),
                Metric("alert_volume", 120.0),
            ]
        )
    ).decode()

    assert 'txrisk_monitoring_psi{feature="seconds_since_sim_swap"} 0.31' in text
    assert 'txrisk_monitoring_label_recall{dimension="channel",segment="ussd"} 0.5' in text
    assert "txrisk_monitoring_alert_volume 120.0" in text


class Gateway(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[str, str, bytes]]] = []

    def do_PUT(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).received.append((self.command, self.path, body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def gateway() -> Iterator[str]:
    Gateway.received = []
    server = HTTPServer(("127.0.0.1", 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_metrics_are_pushed_as_one_group_replaced_each_run(gateway: str):
    push(gateway, [Metric("psi", 0.3, member="log_amount")])

    [(method, path, body)] = Gateway.received
    assert (method, path) == ("PUT", "/metrics/job/txrisk_monitor")
    assert b'txrisk_monitoring_psi{feature="log_amount"} 0.3' in body


def test_a_gateway_that_cannot_be_reached_is_an_error_the_caller_can_catch():
    with pytest.raises(OSError, match="Pushgateway"):
        push("http://127.0.0.1:9", [Metric("alert_volume", 1.0)], timeout=1)


# --- the webhook sink --------------------------------------------------------------------------


PAYLOAD = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "InputDriftAction", "severity": "action", "feature": "x"},
            "annotations": {"summary": "x has drifted"},
            "startsAt": "2026-04-11T00:00:00Z",
        },
        {"status": "resolved", "labels": {"alertname": "AlertVolumeOutsideBudgetBand"}},
    ],
}


def test_the_sink_counts_each_alert_by_name_status_and_severity():
    sink = AlertSink()

    assert sink.receive(PAYLOAD) == 2

    text = sink.metrics().decode()
    assert (
        'txrisk_alert_sink_received_total{alertname="InputDriftAction",severity="action",'
        'status="firing"} 1.0' in text
    )
    assert 'alertname="AlertVolumeOutsideBudgetBand",severity="none",status="resolved"' in text
    assert [a["labels"]["alertname"] for a in sink.recent()] == [
        "InputDriftAction",
        "AlertVolumeOutsideBudgetBand",
    ]


def test_the_sink_serves_what_alertmanager_posts_and_prometheus_scrapes():
    import urllib.error
    import urllib.request

    sink = AlertSink()
    server = serve(sink, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = urllib.request.Request(
            f"{base}/alerts",
            data=json.dumps(PAYLOAD).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response) == {"received": 2}
        with urllib.request.urlopen(f"{base}/metrics") as response:
            assert b"txrisk_alert_sink_received_total" in response.read()
        with urllib.request.urlopen(f"{base}/alerts") as response:
            assert len(json.load(response)) == 2
        bad = urllib.request.Request(f"{base}/alerts", data=b"not json", method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(bad)
        assert refused.value.code == 400
    finally:
        server.shutdown()


# --- the scheduler fires the job once per simulated day ----------------------------------------


def test_the_monitor_job_is_scheduled_daily_to_run_the_monitor_command():
    config = load_scheduler_config(ROOT / "config" / "scheduler.toml")

    [job] = [j for j in config.jobs if j.name == "model-monitor"]

    assert job.schedule == "daily"
    assert job.command == ("txrisk", "monitor")


def test_the_scheduler_fires_the_monitor_once_per_simulated_day(database: str):
    engine = create_engine(database)
    clock = SimulationClock(engine)
    launcher = FakeLauncher()
    jobs = tuple(
        j
        for j in load_scheduler_config(ROOT / "config" / "scheduler.toml").jobs
        if j.name == "model-monitor"
    )
    scheduler = Scheduler(engine, clock, launcher, jobs)
    start = datetime(2026, 4, 11, 0, 0, 5, tzinfo=UTC)

    # Three simulated days, the clock checked many times within each.
    for hours in range(0, 72, 3):
        clock.advance(start + timedelta(hours=hours))
        scheduler.tick()
        scheduler.tick()

    assert launcher.launched == [
        ("model-monitor", date(2026, 4, 11)),
        ("model-monitor", date(2026, 4, 12)),
        ("model-monitor", date(2026, 4, 13)),
    ]
    engine.dispose()


# --- the dashboards ----------------------------------------------------------------------------


def dashboard(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((DASHBOARDS / name).read_text())
    return loaded


def targets(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [t for panel in board["panels"] for t in panel.get("targets", [])]


@pytest.mark.parametrize(
    ("file", "title", "uid"),
    [
        ("model-health.json", "Model health", "txrisk-model-health"),
        ("fraud-outcomes.json", "Fraud outcomes", "txrisk-fraud-outcomes"),
    ],
)
def test_the_dashboards_are_provisioned_with_their_names(file: str, title: str, uid: str):
    board = dashboard(file)

    assert (board["title"], board["uid"]) == (title, uid)
    assert board["editable"] is False
    provider = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "dashboards" / "dashboards.yml").read_text()
    )["providers"][0]
    assert provider["options"]["path"] == "/var/lib/grafana/dashboards"
    assert (
        "./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro"
        in COMPOSE["grafana"]["volumes"]
    )


@pytest.mark.parametrize("file", ["model-health.json", "fraud-outcomes.json"])
def test_every_series_a_monitoring_dashboard_queries_is_one_that_exists(file: str):
    sink_names = {
        family.name + ("_total" if family.type == "counter" else "")
        for family in AlertSink().registry.collect()
    }
    exposed = exposition.exposed_names() | sink_names | {"ALERTS"}
    expressions = [t["expr"] for t in targets(dashboard(file))]

    assert expressions
    used = {
        name for expr in expressions for name in re.findall(r"\b(?:txrisk_[a-z_]+|ALERTS)\b", expr)
    }
    assert used <= exposed, sorted(used - exposed)


@pytest.mark.parametrize("file", ["model-health.json", "fraud-outcomes.json"])
def test_the_monitoring_dashboards_only_use_the_provisioned_prometheus(file: str):
    provisioned = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text()
    )["datasources"]
    uids = {d["uid"] for d in provisioned}
    board = dashboard(file)

    used = {t["datasource"]["uid"] for t in targets(board)}
    used |= {p["datasource"]["uid"] for p in board["panels"] if "datasource" in p}

    assert used and used <= uids
    ids = [p["id"] for p in board["panels"]]
    assert len(ids) == len(set(ids))
    for panel in board["panels"]:
        assert panel["gridPos"]["w"] <= 24


def test_model_health_shows_what_the_ticket_asks_for():
    board = dashboard("model-health.json")
    titles = " | ".join(p["title"].lower() for p in board["panels"])
    queries = " ".join(t["expr"] for t in targets(board))

    for wanted in (
        "psi by model input",
        "risk score psi",
        "risk band mix",
        "alert volume vs budget",
    ):
        assert wanted in titles
    assert "by segment" in titles
    assert 'feature!="risk_score"' in queries and 'feature="risk_score"' in queries
    for dimension in ("label_precision", "label_recall", "label_fpr"):
        assert dimension in queries
    assert "txrisk_alert_sink_received_total" in queries  # the alerts the sink received


def test_fraud_outcomes_shows_discoveries_by_channel_the_complaint_share_and_recall():
    board = dashboard("fraud-outcomes.json")
    titles = " | ".join(p["title"].lower() for p in board["panels"])
    queries = " ".join(t["expr"] for t in targets(board))

    assert "discoveries" in titles and "channel" in titles
    assert "complaint" in titles and "case" in titles
    assert "recall against discovered labels" in titles
    assert "txrisk_monitoring_discoveries_by_channel" in queries
    assert 'discovery_share{source="complaint"}' in queries
    assert "txrisk_monitoring_recall_discovered" in queries


def test_the_dashboards_read_prometheus_alone_so_grafana_holds_no_database_credential():
    provisioned = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text()
    )["datasources"]

    assert {d["type"] for d in provisioned} == {"prometheus", "jaeger"}
