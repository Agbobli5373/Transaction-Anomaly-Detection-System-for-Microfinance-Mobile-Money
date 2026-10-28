"""Evaluation reports and model cards as the tables and curves the pages draw (pure functions)."""

from typing import Any

import pandas as pd

# The headline numbers of an evaluation report, as (label, path into the report).
HEADLINE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PR-AUC", ("ranking", "pr_auc")),
    ("ROC-AUC", ("ranking", "roc_auc")),
    ("Precision at High", ("at_high", "precision")),
    ("Recall at High (Ground truth)", ("at_high", "recall")),
    ("Recall at High (discovered labels)", ("recall_against", "discovered_labels")),
    ("False positive rate at High", ("at_high", "false_positive_rate")),
    ("High Alerts a day", ("at_high", "per_day")),
    ("Precision at the Alert budget", ("at_alert_budget", "precision")),
)


def _dig(report: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = report
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return None if value is None else float(value)


def headline(report: dict[str, Any]) -> pd.Series:
    """The headline metrics of one evaluation report, by label (NaN where it has none)."""
    return pd.Series({label: _dig(report, path) for label, path in HEADLINE}, dtype="float64")


def compare_headlines(first: dict[str, Any], second: dict[str, Any]) -> pd.DataFrame:
    """The headline metrics of two reports side by side, with the second minus the first."""
    table = pd.DataFrame({"A": headline(first), "B": headline(second)})
    table["B minus A"] = table["B"] - table["A"]
    return table


def curve_frame(reports: dict[str, dict[str, Any]], kind: str) -> pd.DataFrame:
    """The `kind` curve ("pr" or "roc") of each report, one column per version, indexed by x.

    Versions are sampled at different points, so the union of the points is used and gaps are
    filled by holding the nearest value.
    """
    columns: dict[str, pd.Series] = {}
    for version, report in reports.items():
        curve = report.get("curves", {}).get(kind)
        if curve:
            points = pd.Series(curve["y"], index=curve["x"], dtype="float64")
            columns[version] = points[~points.index.duplicated(keep="last")]
    if not columns:
        return pd.DataFrame()
    return pd.DataFrame(columns).sort_index().ffill().bfill()


def scenario_recall(reports: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Recall at High for each Fraud scenario (rows) and version (columns), with the number of
    Fraud Transactions in the scenario from the first report."""
    frame = pd.DataFrame(
        {
            version: {
                name: row["recall_at_high"]
                for name, row in report.get("recall_by_scenario", {}).items()
            }
            for version, report in reports.items()
        }
    )
    first = next(iter(reports.values()), {})
    frame.insert(
        0,
        "Fraud",
        pd.Series({n: r["fraud"] for n, r in first.get("recall_by_scenario", {}).items()}),
    )
    return frame


def segment_table(report: dict[str, Any], segment: str) -> pd.DataFrame:
    """The groups of one segment (region, kyc_tier, channel, wallet_age_band) with their
    Transactions, Fraud, and precision, recall and false positive rate at High."""
    return pd.DataFrame.from_dict(report.get("segments", {}).get(segment, {}), orient="index")


def segment_comparison(
    reports: dict[str, dict[str, Any]], segment: str, metric: str
) -> pd.DataFrame:
    """One metric of each group of a segment, a column per version."""
    return pd.DataFrame(
        {
            version: segment_table(report, segment).get(metric, pd.Series(dtype="float64"))
            for version, report in reports.items()
        }
    )


def card_overall(card: dict[str, Any]) -> pd.Series | None:
    """The model card's overall metrics (on the calibration days), or None if not recorded."""
    metrics = card.get("metrics", {})
    if metrics.get("recorded") is False or "overall" not in metrics:
        return None
    return pd.Series(metrics["overall"], dtype="float64")


def card_segments(card: dict[str, Any], segment: str) -> pd.DataFrame:
    """The model card's groups of one segment, on the calibration days."""
    metrics = card.get("metrics", {})
    return pd.DataFrame.from_dict(metrics.get("by_segment", {}).get(segment, {}), orient="index")
