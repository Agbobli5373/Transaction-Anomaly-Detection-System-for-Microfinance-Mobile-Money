"""The Anomaly score: how unusual a Transaction looks, judged without any labels.

An unsupervised model learns what the training period looks like, and each new Transaction is
scored as the percentile of its unusualness among the training Transactions: 0.99 means more
unusual than 99% of them. It measures unusualness, not guilt.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.neighbors import LocalOutlierFactor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import RobustScaler

from txrisk.models.inputs import INPUT_COLUMNS

# Empty features (cold start, no SIM swap) sit below every real value, which is itself telling.
_MISSING = -1.0


class _Unusualness(Protocol):
    def score_samples(self, x: np.ndarray) -> np.ndarray: ...


def _matrix(inputs: pd.DataFrame) -> np.ndarray:
    return inputs[INPUT_COLUMNS].fillna(_MISSING).to_numpy(dtype=np.float64)


@dataclass(frozen=True)
class AnomalyModel:
    """A fitted unsupervised model plus the unusualness of its training Transactions."""

    method: str
    estimator: _Unusualness
    training_unusualness: np.ndarray  # sorted ascending

    @classmethod
    def fit(cls, inputs: pd.DataFrame, *, seed: int, trees: int = 200) -> "AnomalyModel":
        """An Isolation Forest fitted on every training Transaction, labels unseen."""
        forest = IsolationForest(n_estimators=trees, random_state=seed)
        matrix = _matrix(inputs)
        forest.fit(matrix)
        return cls("isolation_forest", forest, np.sort(-forest.score_samples(matrix)))

    @classmethod
    def fit_lof(
        cls, inputs: pd.DataFrame, *, seed: int, sample: int = 20_000, neighbours: int = 20
    ) -> "AnomalyModel":
        """Local Outlier Factor on a sample of the training Transactions.

        Offline comparison only: it compares every scored Transaction against its neighbours, so
        scoring is too slow and memory-hungry to serve. Being distance-based, it needs every input
        on a comparable scale, so each is centred on its median and divided by its spread first.
        """
        rows = inputs.sample(min(sample, len(inputs)), random_state=seed)
        matrix = _matrix(rows)
        lof = make_pipeline(
            RobustScaler(), LocalOutlierFactor(n_neighbors=neighbours, novelty=True)
        )
        lof.fit(matrix)
        return cls("local_outlier_factor", lof, np.sort(-lof.score_samples(matrix)))

    def score(self, inputs: pd.DataFrame) -> pd.Series:
        """The Anomaly score of each Transaction, in [0, 1], indexed like `inputs`."""
        unusualness = -self.estimator.score_samples(_matrix(inputs))
        rank = np.searchsorted(self.training_unusualness, unusualness, side="right")
        return pd.Series(
            rank / len(self.training_unusualness), index=inputs.index, name="anomaly_score"
        )
