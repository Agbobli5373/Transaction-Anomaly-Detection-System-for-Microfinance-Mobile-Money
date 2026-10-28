"""The metrics `txrisk monitor` computes, and how they reach Prometheus.

A job is a one-off container that exits in seconds, so Prometheus cannot scrape it. It pushes the
latest day's metrics to a Pushgateway (compose network only), which Prometheus scrapes and which
keeps the last values until the next push; each push replaces the whole group, so a segment that
has gone leaves no stale series. The history stays in Postgres (`monitoring_metrics`).

Every metric is a gauge named `txrisk_monitoring_<name>`. A `Metric` carries at most two
dimensions, stored as `dimension` and `member`; the spec of its name says which Prometheus labels
they become: one label means the member alone, two mean (dimension, member).
"""

import logging
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Gauge, generate_latest

log = logging.getLogger(__name__)

PREFIX = "txrisk_monitoring_"
PUSH_JOB = "txrisk_monitor"

# Where a metric spans the whole population rather than one segment.
ALL = "all"


@dataclass(frozen=True)
class Spec:
    help: str
    labels: tuple[str, ...] = ()


SPECS: dict[str, Spec] = {
    "day": Spec("The monitored simulated day, as a Unix time (its start)"),
    "info": Spec("The Champion model the day was monitored for", ("model_version",)),
    "baseline_available": Spec("1 if the Champion model's bundle carries a training baseline"),
    "scored_rows": Spec("Transactions the Champion model scored on the day, with their inputs"),
    "psi": Spec(
        "Population stability index of a model input or the Risk score against the baseline",
        ("feature",),
    ),
    "psi_alerted": Spec(
        "PSI of a model input among the High Transactions of the last days, against the baseline's",
        ("feature",),
    ),
    "alerted_rows": Spec("High Transactions of the last days that psi_alerted is measured on"),
    "psi_warn_threshold": Spec("PSI at which a shift is a warning (config/monitoring.toml)"),
    "psi_action_threshold": Spec("PSI at which a shift needs action (config/monitoring.toml)"),
    "alert_volume": Spec("Alerts raised on the day"),
    "alert_budget": Spec("The Alert budget per day"),
    "alert_volume_ratio": Spec("Alert volume as a share of the Alert budget"),
    "alert_volume_ratio_min": Spec("The least share of the budget the volume may be"),
    "alert_volume_ratio_max": Spec("The most share of the budget the volume may be"),
    "risk_band_count": Spec("Assessments by Risk band on the day", ("band",)),
    "risk_band_share": Spec("Share of the day's assessments by Risk band", ("band",)),
    "label_transactions": Spec(
        "Matured Transactions judged against the labels known", ("dimension", "segment")
    ),
    "label_positives": Spec(
        "Matured Transactions with a Fraud label known at the time", ("dimension", "segment")
    ),
    "label_precision": Spec("Precision at High on matured Transactions", ("dimension", "segment")),
    "label_recall": Spec("Recall at High on matured Transactions", ("dimension", "segment")),
    "label_fpr": Spec(
        "False positive rate at High on matured Transactions", ("dimension", "segment")
    ),
    "label_drop_precision": Spec(
        "How far precision fell below its recent mean, as a share of that mean",
        ("dimension", "segment"),
    ),
    "label_drop_recall": Spec(
        "How far recall fell below its recent mean, as a share of that mean",
        ("dimension", "segment"),
    ),
    "label_drop_threshold": Spec("The relative drop that counts as a regression"),
    "discoveries_by_source": Spec(
        "Fraud discoveries in the window, by source (complaint, chargeback, case)", ("source",)
    ),
    "discovery_share": Spec("Share of the window's Fraud discoveries by source", ("source",)),
    "discoveries_by_channel": Spec("Fraud discoveries in the window, by Channel", ("channel",)),
    "discoveries_day": Spec("Fraud discoveries on the day, by Channel", ("channel",)),
    "discovered_scored": Spec("Fraud-labelled Transactions that were scored, as of the day"),
    "discovered_high": Spec("... of which were High"),
    "recall_discovered": Spec("Share of Fraud-labelled, scored Transactions that were High"),
}


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    dimension: str = ""
    member: str = ""

    def __post_init__(self) -> None:
        if self.name not in SPECS:
            raise ValueError(f"unknown metric {self.name}")


def registry_of(metrics: Iterable[Metric]) -> CollectorRegistry:
    """The metrics as Prometheus gauges in a registry of their own."""
    registry = CollectorRegistry()
    gauges: dict[str, Gauge] = {}
    for metric in metrics:
        spec = SPECS[metric.name]
        if metric.name not in gauges:
            gauges[metric.name] = Gauge(
                PREFIX + metric.name, spec.help, list(spec.labels), registry=registry
            )
        values = (metric.dimension, metric.member)[2 - len(spec.labels) :]
        gauge = gauges[metric.name]
        (gauge.labels(*values) if spec.labels else gauge).set(metric.value)
    return registry


def exposed_names() -> set[str]:
    """Every series name the monitor can push."""
    return {PREFIX + name for name in SPECS}


def push(url: str, metrics: Iterable[Metric], *, timeout: float = 10.0) -> None:
    """Replace the monitor's group on the Pushgateway at `url` with these metrics. Raises
    `OSError` if the gateway cannot be reached or refuses."""
    body = generate_latest(registry_of(metrics))
    request = urllib.request.Request(
        f"{url.rstrip('/')}/metrics/job/{PUSH_JOB}",
        data=body,
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except urllib.error.URLError as e:
        raise OSError(f"could not push to the Pushgateway at {url}: {e}") from e
