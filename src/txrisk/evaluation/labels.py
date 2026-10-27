"""Time-based splits, and the training labels a real operation would have at a cutoff."""

from datetime import date
from enum import StrEnum

import numpy as np
import pandas as pd

from txrisk.evaluation.config import EvaluationConfig


class Split(StrEnum):
    TRAIN = "train"
    CALIBRATION = "calibration"
    THRESHOLDS = "thresholds"
    EVALUATION = "evaluation"


def split_of(occurred_at: pd.Series, period_start: date, config: EvaluationConfig) -> pd.Series:
    """The split each Transaction belongs to, by the day it occurred on."""
    days = (occurred_at - pd.Timestamp(period_start, tz="UTC")) / pd.Timedelta(days=1)
    split = pd.Series(pd.NA, index=occurred_at.index, dtype="object")
    for name in Split:
        window = getattr(config.splits, name.value)
        split[(days >= window.start) & (days < window.end)] = name
    return split


def training_labels(
    transactions: pd.DataFrame,
    discoveries: pd.DataFrame,
    *,
    cutoff: pd.Timestamp,
    config: EvaluationConfig,
) -> pd.Series:
    """Labels known at `cutoff`, indexed by transaction_id: 1 for fraud, 0 for legitimate.

    Only Fraud discoveries made before the cutoff count as fraud. An undiscovered Transaction
    counts as legitimate only once it is older than the Label maturity window; younger ones may
    still turn out to be fraud, so they are left out.
    """
    before = transactions[transactions["occurred_at"] < cutoff]
    known = discoveries.loc[discoveries["discovered_at"] < cutoff, "transaction_id"]
    fraud = before["transaction_id"].isin(set(known))
    matured = before["occurred_at"] < cutoff - pd.Timedelta(days=config.label_maturity_days)
    kept = fraud | matured
    return pd.Series(
        np.where(fraud[kept], 1, 0),
        index=pd.Index(before.loc[kept, "transaction_id"], name="transaction_id"),
        name="label",
    )
