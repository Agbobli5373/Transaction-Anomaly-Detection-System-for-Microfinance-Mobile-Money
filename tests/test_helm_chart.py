"""The Helm chart (deploy/helm/txrisk), rendered with `helm template`: every service has probes and
resource limits, the scoring workers autoscale, and no credential is in the chart. Behaviour in a
cluster is checked by the `kubernetes` CI job and scripts/kind-smoke.sh."""

import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "deploy" / "helm" / "txrisk"
WORKLOADS = ("Deployment", "StatefulSet")


def helm_template(*settings: str) -> list[dict[str, Any]]:
    if shutil.which("helm") is None:
        if os.environ.get("CI"):
            pytest.fail("helm is not installed")
        pytest.skip("helm is not installed")
    result = subprocess.run(
        ["helm", "template", "txrisk", str(CHART), "--namespace", "txrisk", *settings],
        capture_output=True,
        text=True,
        check=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    return helm_template()


def of_kind(docs: list[dict[str, Any]], *kinds: str) -> Iterator[dict[str, Any]]:
    return (d for d in docs if d["kind"] in kinds)


def containers(workload: dict[str, Any]) -> list[dict[str, Any]]:
    pod = workload["spec"]["template"]["spec"]
    return [*pod.get("initContainers", []), *pod["containers"]]


def test_every_service_deploys_with_readiness_and_liveness_probes_and_resource_limits(
    rendered: list[dict[str, Any]],
):
    names = {d["metadata"]["name"] for d in of_kind(rendered, *WORKLOADS)}
    assert names >= {
        "postgres", "redis", "redpanda", "keycloak", "api", "worker", "outbox", "workbench",
        "scheduler", "prometheus", "pushgateway", "alertmanager", "alert-sink", "grafana",
        "jaeger",
    }  # fmt: skip
    for workload in of_kind(rendered, *WORKLOADS):
        (main, *_) = workload["spec"]["template"]["spec"]["containers"]
        name = workload["metadata"]["name"]
        assert "readinessProbe" in main, name
        assert "livenessProbe" in main, name
        for container in containers(workload):
            assert container["resources"]["limits"]["memory"], (name, container["name"])
            assert container["resources"]["requests"]["cpu"], (name, container["name"])


def test_the_migration_job_runs_the_txrisk_image_with_resource_limits(
    rendered: list[dict[str, Any]],
):
    (job,) = (d for d in of_kind(rendered, "Job") if d["metadata"]["name"].startswith("migrate-"))

    for container in containers(job):
        assert container["resources"]["limits"]["memory"]
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == ["txrisk", "migrate"]


def test_the_scoring_workers_autoscale_on_cpu_load_within_the_topics_partitions(
    rendered: list[dict[str, Any]],
):
    (autoscaler,) = of_kind(rendered, "HorizontalPodAutoscaler")
    (worker,) = (d for d in of_kind(rendered, "Deployment") if d["metadata"]["name"] == "worker")

    assert autoscaler["spec"]["scaleTargetRef"]["name"] == "worker"
    assert autoscaler["spec"]["maxReplicas"] > autoscaler["spec"]["minReplicas"]
    # transaction.created has three partitions by default; workers beyond them would idle.
    assert autoscaler["spec"]["maxReplicas"] <= 3
    assert autoscaler["spec"]["metrics"][0]["resource"]["name"] == "cpu"
    # The autoscaler owns the replica count, so an upgrade does not reset it.
    assert "replicas" not in worker["spec"]


def test_autoscaling_can_be_turned_off_for_a_fixed_number_of_workers():
    docs = helm_template("--set", "worker.autoscaling.enabled=false", "--set", "worker.replicas=2")

    assert not list(of_kind(docs, "HorizontalPodAutoscaler"))
    (worker,) = (d for d in of_kind(docs, "Deployment") if d["metadata"]["name"] == "worker")
    assert worker["spec"]["replicas"] == 2


def test_every_credential_reaches_a_pod_from_a_secret_that_the_chart_does_not_create(
    rendered: list[dict[str, Any]],
):
    assert not list(of_kind(rendered, "Secret"))
    secret_names = {
        ref["secretRef"]["name"]
        for workload in of_kind(rendered, *WORKLOADS, "Job")
        for container in containers(workload)
        for ref in container.get("envFrom", [])
    }
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert secret_names == set(values["settingsFrom"].values())


def test_the_api_and_workers_connect_as_the_application_role_and_the_owner_stays_with_jobs(
    rendered: list[dict[str, Any]],
):
    def database_url(workload: dict[str, Any]) -> str:
        env = {e["name"]: e.get("value") for c in containers(workload) for e in c.get("env", [])}
        return str(env["TXRISK_DATABASE_URL"])

    workloads = {d["metadata"]["name"]: d for d in of_kind(rendered, "Deployment", "Job")}
    for service in ("api", "worker", "outbox", "scheduler"):
        assert database_url(workloads[service]) == "postgresql://txrisk_app@postgres:5432/txrisk"
    (migrate,) = (d for n, d in workloads.items() if n.startswith("migrate-"))
    assert database_url(migrate) == "postgresql://txrisk@postgres:5432/txrisk"


def test_no_credential_is_written_in_the_chart_or_the_manifests_it_renders(
    rendered: list[dict[str, Any]],
):
    files = [p for p in CHART.rglob("*") if p.is_file() and not p.is_symlink()]
    text = "\n".join(p.read_text() for p in files)
    # A URL with a login and a password in it.
    assert not re.search(r"://[^/\s:@]+:[^/\s@]+@", text)
    # A password-, secret- or token-named key that is given a value.
    given = re.compile(r"^\s*[\w-]*(password|secret|token)[\w-]*[ \t]*[:=][ \t]*\S", re.I | re.M)
    assert not given.findall(text)
    for doc in of_kind(rendered, *WORKLOADS, "Job"):
        for container in containers(doc):
            for env in container.get("env", []):
                assert not re.search(r"password|secret|token", env["name"], re.I), env["name"]


def test_the_scheduler_launches_jobs_in_this_releases_namespace_with_the_models_volume():
    docs = helm_template("--namespace", "risk", "--set", "image.txrisk.tag=ci")

    (config,) = (
        d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == "scheduler-config"
    )
    kubernetes = tomllib.loads(config["data"]["kubernetes.toml"])["kubernetes"]
    assert kubernetes["namespace"] == "risk"
    assert kubernetes["image"] == "txrisk:ci"
    assert kubernetes["models_claim"] == "models"
    assert "TXRISK_PUSHGATEWAY_URL" in kubernetes["env"]
    # The jobs' credential is the database Secret, by name only.
    assert kubernetes["env_from"] == "txrisk-database"


def test_prometheus_finds_every_replica_of_a_scaled_service_through_its_metrics_service(
    rendered: list[dict[str, Any]],
):
    metrics = {
        d["metadata"]["name"]: d
        for d in of_kind(rendered, "Service")
        if d["metadata"]["name"].endswith("-metrics")
    }
    assert {"api-metrics", "worker-metrics", "outbox-metrics"} <= set(metrics)
    assert all(d["spec"]["clusterIP"] == "None" for d in metrics.values())
    (config,) = (
        d for d in of_kind(rendered, "ConfigMap") if d["metadata"]["name"] == "prometheus-config"
    )
    scrape = yaml.safe_load(config["data"]["prometheus.yml"])["scrape_configs"]
    names = {n for job in scrape for sd in job.get("dns_sd_configs", []) for n in sd["names"]}
    assert "worker-metrics.txrisk.svc.cluster.local" in names
