"""Splits by time, and training labels from Fraud discoveries alone (ADR 0007)."""

from datetime import date
from pathlib import Path

import pandas as pd

from txrisk.evaluation import Split, load_evaluation_config, split_of, training_labels

CONFIG = load_evaluation_config(Path(__file__).resolve().parents[1] / "config" / "evaluation.toml")
START = date(2026, 1, 1)


def _at(day: float) -> pd.Timestamp:
    return pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=day)


def test_each_transaction_falls_in_the_split_for_its_day():
    occurred = pd.Series([_at(0), _at(59.99), _at(60), _at(74.5), _at(75), _at(89.9), _at(90)])

    assert split_of(occurred, START, CONFIG).tolist() == [
        Split.TRAIN,
        Split.TRAIN,
        Split.CALIBRATION,
        Split.CALIBRATION,
        Split.THRESHOLDS,
        Split.THRESHOLDS,
        Split.EVALUATION,
    ]


def test_training_labels_come_only_from_what_was_discovered_by_the_cutoff():
    cutoff = _at(60)
    transactions = pd.DataFrame(
        {
            "transaction_id": [
                "old-found",
                "old-quiet",
                "old-found-late",
                "new-found",
                "new-quiet",
            ],
            "occurred_at": [_at(10), _at(20), _at(25), _at(50), _at(55)],
        }
    )
    discoveries = pd.DataFrame(
        {
            "transaction_id": ["old-found", "old-found-late", "new-found"],
            "discovered_at": [_at(15), _at(61), _at(52)],
        }
    )

    labels = training_labels(transactions, discoveries, cutoff=cutoff, config=CONFIG)

    # Discovered by the cutoff: fraud. Undiscovered and older than 30 days: taken as legitimate,
    # even if it is later found to be fraud. Undiscovered and recent: not yet known, left out.
    assert labels.to_dict() == {"old-found": 1, "old-quiet": 0, "old-found-late": 0, "new-found": 1}


def test_nothing_on_the_running_side_reads_ground_truth():
    """Only the generator writes Ground truth and only the evaluation report reads it."""
    src = Path(__file__).resolve().parents[1] / "src" / "txrisk"
    allowed = {src / "generator", src / "evaluation" / "report.py"}

    readers = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "ground_truth" in path.read_text(encoding="utf-8")
        and not any(path == a or a in path.parents for a in allowed)
    ]

    assert readers == []
