"""Population stability index, and the training baseline it is measured against.

A baseline is a column's distribution in the rows a model was built from: quantile bin edges and
the share of rows in each bin, plus a share for missing values (a feature is empty on cold start,
or without a SIM swap, and how often is part of its distribution). It is small and fixed in size
however many rows made it, so it travels inside the model bundle.

PSI = sum over bins of (live share - baseline share) x ln(live share / baseline share). Below 0.1
is stable, 0.1 to 0.25 a shift worth a look, and above 0.25 a shift that needs action.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# A bin nobody falls in would make the logarithm blow up; its share counts as at least this.
_FLOOR = 1e-4


@dataclass(frozen=True)
class Distribution:
    """One column's baseline. `edges` are ascending cut points: bin i holds values above
    `edges[i-1]` and up to `edges[i]`, and the last bin everything above the last edge, so
    `len(edges) + 1` bins. `shares` has one share per bin, then the share of missing values."""

    edges: tuple[float, ...]
    shares: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.shares) != len(self.edges) + 2:
            raise ValueError("a distribution has one share per bin and one for missing values")

    def shares_of(self, values: pd.Series | np.ndarray) -> np.ndarray:
        """The share of `values` in each bin, then of missing ones."""
        x = np.asarray(values, dtype=np.float64)
        missing = ~np.isfinite(x)
        bins = np.searchsorted(np.asarray(self.edges), x[~missing], side="left")
        counts = np.bincount(bins, minlength=len(self.edges) + 1).astype(np.float64)
        return np.append(counts, missing.sum()) / max(len(x), 1)


def fit_distribution(values: pd.Series | np.ndarray, bins: int) -> Distribution:
    """The baseline of `values`: `bins` quantile bins (fewer where values repeat), and the
    share of missing values."""
    x = np.asarray(values, dtype=np.float64)
    finite = x[np.isfinite(x)]
    edges: tuple[float, ...] = ()
    if len(finite):
        quantiles = np.quantile(finite, np.linspace(0, 1, bins + 1)[1:-1])
        edges = tuple(float(e) for e in np.unique(quantiles))
    shares = Distribution(edges, (0.0,) * (len(edges) + 2)).shares_of(x)
    return Distribution(edges, tuple(float(s) for s in shares))


def psi(baseline: Distribution, values: pd.Series | np.ndarray) -> float:
    """The population stability index of `values` against `baseline`."""
    expected = np.maximum(np.asarray(baseline.shares), _FLOOR)
    actual = np.maximum(baseline.shares_of(values), _FLOOR)
    return float(np.sum((actual - expected) * np.log(actual / expected)))


@dataclass(frozen=True)
class Baseline:
    """What a Champion model was trained on, as far as monitoring needs: the distribution of
    every model input and of the Risk score, from rows the bundle was built from and never from
    later data. `rows` is how many rows made it.

    `alerted` is the same inputs among the rows that were High, the ones that became Alerts.
    Fraud is a tiny share of all Transactions, so a change in how fraud looks barely moves the
    population's distribution but does move the Alerts', which is where it concentrates."""

    rows: int
    inputs: Mapping[str, Distribution]
    risk_score: Distribution
    alerted_rows: int = 0
    alerted: Mapping[str, Distribution] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        def one(d: Distribution) -> dict[str, list[float]]:
            return {"edges": list(d.edges), "shares": list(d.shares)}

        return {
            "rows": self.rows,
            "inputs": {name: one(d) for name, d in self.inputs.items()},
            "risk_score": one(self.risk_score),
            "alerted_rows": self.alerted_rows,
            "alerted": {name: one(d) for name, d in self.alerted.items()},
        }


def fit_baseline(
    inputs: pd.DataFrame,
    risk_scores: pd.Series,
    *,
    bins: int,
    alerted: np.ndarray | pd.Series | None = None,
) -> Baseline:
    """The baseline of these rows' model inputs (every column of `inputs`) and Risk scores, and
    of the inputs of the rows `alerted` marks (those that were High)."""
    picked = np.zeros(len(inputs), dtype=bool) if alerted is None else np.asarray(alerted, bool)
    return Baseline(
        rows=len(inputs),
        inputs={str(c): fit_distribution(inputs[c], bins) for c in inputs.columns},
        risk_score=fit_distribution(risk_scores, bins),
        alerted_rows=int(picked.sum()),
        alerted=(
            {str(c): fit_distribution(inputs.loc[picked, c], bins) for c in inputs.columns}
            if picked.any()
            else {}
        ),
    )
