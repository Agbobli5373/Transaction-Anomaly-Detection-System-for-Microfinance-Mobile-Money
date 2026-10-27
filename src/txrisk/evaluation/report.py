"""The evaluation report: the only place outside the generator that reads Ground truth.

Ground truth is the simulation's complete knowledge of fraud (ADR 0007). Training, scoring and
monitoring never see it; this report uses it to say how much fraud really happened and how much
of it a scorer caught, alongside what the operation would have discovered by itself.
"""

import itertools
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # files only, no display
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.metrics import precision_recall_curve, roc_curve

from txrisk.evaluation.config import EvaluationConfig
from txrisk.evaluation.labels import Split, split_of
from txrisk.evaluation.metrics import at_threshold, precision_at_budget, ranking


def _wallet_age_band(age_days: pd.Series, edges: list[int]) -> pd.Series:
    labels = [f"<{edges[0]}d"]
    labels += [f"{low}-{high}d" for low, high in itertools.pairwise(edges)]
    labels += [f">={edges[-1]}d"]
    bands = pd.cut(age_days, [-float("inf"), *edges, float("inf")], right=False, labels=labels)
    return bands.astype(str)


def _segments(frame: pd.DataFrame, flagged: pd.Series) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for segment in ("region", "kyc_tier", "channel", "wallet_age_band"):
        groups: dict[str, Any] = {}
        for value, rows in frame.groupby(segment, sort=True):
            metrics = at_threshold(rows["fraud"], flagged[rows.index])
            groups[str(value)] = {
                "transactions": len(rows),
                "fraud": int(rows["fraud"].sum()),
                **asdict(metrics),
            }
        result[segment] = groups
    return result


def _plot(frame: pd.DataFrame, out: Path, scorer: str) -> None:
    if frame["fraud"].nunique() < 2:
        return
    precision, recall, _ = precision_recall_curve(frame["fraud"], frame["risk_score"])
    false_positive_rate, true_positive_rate, _ = roc_curve(frame["fraud"], frame["risk_score"])
    for name, x, y, x_label, y_label in (
        ("pr_curve", recall, precision, "Recall", "Precision"),
        ("roc_curve", false_positive_rate, true_positive_rate, "False positive rate", "Recall"),
    ):
        figure, axes = plt.subplots(figsize=(5, 4))
        axes.plot(x, y)
        axes.set(xlabel=x_label, ylabel=y_label, xlim=(0, 1), ylim=(0, 1.02), title=scorer)
        axes.grid(alpha=0.3)
        figure.tight_layout()
        figure.savefig(out / f"{name}.png", dpi=120)
        plt.close(figure)


def evaluate(
    scores: pd.DataFrame,
    data: Path,
    *,
    config: EvaluationConfig,
    period_start: date,
    scorer: str,
    out: Path,
) -> dict[str, Any]:
    """Judge `scores` (transaction_id, risk_score, risk_band) on the evaluation split.

    Writes metrics.json, the PR and ROC curves and the evaluated scores to `out`, and returns
    the metrics. High is the scorer's own High Risk band.
    """
    transactions = pd.read_parquet(data / "transactions.parquet")
    wallets = pd.read_parquet(data / "wallets.parquet")
    truth = pd.read_parquet(data / "ground_truth.parquet")
    discoveries = pd.read_parquet(data / "fraud_discoveries.parquet")
    window = config.splits.evaluation
    start = pd.Timestamp(period_start, tz="UTC")
    end = start + pd.Timedelta(days=window.end)

    evaluated = transactions[
        split_of(transactions["occurred_at"], period_start, config) == Split.EVALUATION
    ]
    frame = (
        evaluated.merge(scores, on="transaction_id", how="left")
        .merge(truth[["transaction_id", "scenario"]], on="transaction_id", how="left")
        .merge(wallets[["wallet_id", "kyc_tier", "registered_at"]], left_on="subject_wallet_id",
               right_on="wallet_id", how="left")
    )  # fmt: skip
    if frame["risk_score"].isna().any():
        raise ValueError("every Transaction in the evaluation split needs a score")
    frame["fraud"] = frame["scenario"].notna()
    frame["day"] = frame["occurred_at"].dt.floor("D")
    age = (frame["occurred_at"] - frame["registered_at"]).dt.total_seconds() / 86_400
    frame["wallet_age_band"] = _wallet_age_band(age, config.wallet_age_band_edges)
    high = frame["risk_band"] == "High"
    discovered = frame["transaction_id"].isin(
        set(discoveries.loc[discoveries["discovered_at"] < end, "transaction_id"])
    )
    days = window.end - window.start

    at_high = at_threshold(frame["fraud"], high)
    metrics: dict[str, Any] = {
        "scorer": scorer,
        "split": Split.EVALUATION.value,
        "days": [window.start, window.end],
        "transactions": len(frame),
        "fraud": int(frame["fraud"].sum()),
        "fraud_discovered": int((frame["fraud"] & discovered).sum()),
        "ranking": asdict(ranking(frame["fraud"], frame["risk_score"].astype(float))),
        "at_high": {**asdict(at_high), "per_day": at_high.flagged / days},
        "at_medium_or_higher": asdict(at_threshold(frame["fraud"], frame["risk_band"] != "Low")),
        "at_alert_budget": {
            "budget_per_day": config.alert_budget_per_day,
            **asdict(
                precision_at_budget(
                    frame["fraud"],
                    frame["risk_score"].astype(float),
                    frame["day"],
                    budget_per_day=config.alert_budget_per_day,
                )
            ),
        },
        "recall_by_scenario": {
            str(scenario): {"fraud": len(rows), "recall_at_high": float(high[rows.index].mean())}
            for scenario, rows in frame[frame["fraud"]].groupby("scenario", sort=True)
        },
        # Ground truth: everything that really happened. Discovered labels: only the fraud the
        # operation would know about by the end of the evaluation period.
        "recall_against": {
            "ground_truth": at_high.recall,
            "discovered_labels": at_threshold(
                frame.loc[discovered, "fraud"], high[discovered]
            ).recall,
        },
        "segments": _segments(frame, high),
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    frame[scores.columns].to_parquet(out / "scores.parquet", index=False)
    _plot(frame, out, scorer)
    return metrics
