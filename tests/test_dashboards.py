"""The provisioned monitoring files agree with each other and with the code: the Operations
dashboard queries metrics the services expose, through datasources that are provisioned, from
targets Prometheus scrapes on the ports compose gives them, on UIs bound to this machine only."""

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from txrisk.observability.metrics import Metrics

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
DASHBOARD = json.loads((DEPLOY / "grafana" / "dashboards" / "operations.json").read_text())


def exposed_names() -> set[str]:
    """Every series name the services' registry can expose, as a scrape spells it."""
    suffixes = {
        "counter": ["_total"],
        "histogram": ["_bucket", "_count", "_sum"],
        "gauge": [""],
    }
    return {
        family.name + suffix
        for family in Metrics().registry.collect()
        for suffix in suffixes.get(family.type, [""])
    }


def queries() -> list[str]:
    return [t["expr"] for panel in DASHBOARD["panels"] for t in panel["targets"]]


def test_the_operations_dashboard_shows_what_the_ticket_asks_for():
    titles = " | ".join(p["title"].lower() for p in DASHBOARD["panels"])
    for wanted in ("latency percentiles", "throughput", "errors", "consumer lag", "degraded mode"):
        assert wanted in titles
    assert "alerts per day" in titles and "risk band" in titles
    quantiles = " ".join(queries())
    assert all(f"histogram_quantile({q}" in quantiles for q in ("0.50", "0.95", "0.99"))


def test_every_metric_the_dashboard_queries_is_one_a_service_exposes():
    used = {name for q in queries() for name in re.findall(r"\btxrisk_[a-z_]+", q)}
    assert used
    assert used <= exposed_names(), sorted(used - exposed_names())


def test_the_dashboard_only_uses_provisioned_datasources():
    provisioned = yaml.safe_load(
        (DEPLOY / "grafana" / "provisioning" / "datasources" / "datasources.yml").read_text()
    )["datasources"]
    uids = {d["uid"] for d in provisioned}
    used = {p["datasource"]["uid"] for p in DASHBOARD["panels"]} | {
        t["datasource"]["uid"] for p in DASHBOARD["panels"] for t in p["targets"]
    }
    assert used <= uids
    assert {d["type"] for d in provisioned} == {"prometheus", "jaeger"}


def test_prometheus_scrapes_the_metrics_ports_compose_gives_the_services():
    config = yaml.safe_load((DEPLOY / "prometheus" / "prometheus.yml").read_text())
    scraped: dict[str, Any] = {}
    for job in config["scrape_configs"]:
        for found in job.get("dns_sd_configs", []):
            scraped[found["names"][0]] = found["port"]
    for service in ("api", "worker", "replay"):
        assert str(scraped[service]) == COMPOSE[service]["environment"]["TXRISK_METRICS_PORT"]


@pytest.mark.parametrize("service", ["prometheus", "grafana", "jaeger"])
def test_the_monitoring_uis_are_published_on_this_machine_only(service: str):
    assert all(port.startswith("127.0.0.1:") for port in COMPOSE[service]["ports"])


def test_metrics_ports_are_not_published_and_no_credential_is_committed():
    for service in ("api", "worker", "replay", "produce"):
        published = " ".join(map(str, COMPOSE[service].get("ports", [])))
        assert not re.search(r"910[0-2]", published)
    grafana = COMPOSE["grafana"]
    assert "GF_SECURITY_ADMIN_PASSWORD" not in grafana["environment"]
    assert grafana["env_file"] == ".env"
