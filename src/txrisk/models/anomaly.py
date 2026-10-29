"""The Anomaly score: how unusual a Transaction looks, judged without any labels.

An unsupervised model learns what the training period looks like, and each new Transaction is
scored as the percentile of its unusualness among the training Transactions: 0.99 means more
unusual than 99% of them. It measures unusualness, not guilt.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import Any, Protocol

import numpy as np
import pandas as pd
import shap
from sklearn.ensemble import IsolationForest
from sklearn.ensemble._iforest import _average_path_length
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


def fill_missing(matrix: np.ndarray) -> np.ndarray:
    """A matrix of model inputs in `INPUT_COLUMNS` order, with empty ones put where `_matrix`
    puts them."""
    return np.where(np.isnan(matrix), _MISSING, matrix)


class _CompiledForest:
    """An Isolation Forest's trees as flat arrays, walked for every tree at once.

    scikit-learn scores through one Python-level call per tree (200 of them), about 20 ms for a
    single row and no cheaper for the next. This follows the very same splits (a float32 input
    against the float64 threshold, as its trees do) and adds the per-leaf path lengths up in the
    same order, so it returns the same unusualness; it just does the walk for all trees together
    in numpy, which costs about the same for one row as for a few hundred.
    """

    def __init__(self, forest: IsolationForest) -> None:
        trees = [estimator.tree_ for estimator in forest.estimators_]
        offsets = np.cumsum([0, *(tree.node_count for tree in trees[:-1])])
        feature, threshold, left, right, value = [], [], [], [], []
        for tree, offset, path, average in zip(
            trees,
            offsets,
            forest._decision_path_lengths,
            forest._average_path_length_per_tree,
            strict=True,
        ):
            node = np.arange(tree.node_count) + offset
            leaf = tree.children_left == -1
            # A leaf points at itself, so walking on past it changes nothing.
            feature.append(np.where(leaf, 0, tree.feature))
            threshold.append(np.where(leaf, np.inf, tree.threshold))
            left.append(np.where(leaf, node, tree.children_left + offset))
            right.append(np.where(leaf, node, tree.children_right + offset))
            value.append(path + average - 1.0)  # as scikit-learn adds it up for a leaf
        self._feature = np.concatenate(feature)
        self._threshold = np.concatenate(threshold)
        self._left = np.concatenate(left)
        self._right = np.concatenate(right)
        self._value = np.concatenate(value)
        self._roots = offsets
        self._steps = max(tree.max_depth for tree in trees)
        self._denominator = len(trees) * _average_path_length([forest._max_samples])

    def unusualness(self, matrix: np.ndarray) -> np.ndarray:
        """What `-forest.score_samples(matrix)` is, for a matrix of any number of rows."""
        x = matrix.astype(np.float32)
        rows = np.arange(len(x))[:, None]
        at = np.broadcast_to(self._roots, (len(x), len(self._roots)))
        for _ in range(self._steps):
            go_left = x[rows, self._feature[at]] <= self._threshold[at]
            at = np.where(go_left, self._left[at], self._right[at])
        # A running sum over the trees in order, as scikit-learn accumulates it.
        depths = np.cumsum(self._value[at], axis=1)[:, -1]
        return np.asarray(
            2
            ** (
                -np.divide(
                    depths,
                    self._denominator,
                    out=np.ones_like(depths),
                    where=self._denominator != 0,
                )
            ),
            dtype=np.float64,
        )


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

    def _percentile(self, unusualness: np.ndarray) -> np.ndarray:
        rank = np.searchsorted(self.training_unusualness, unusualness, side="right")
        return np.asarray(rank / len(self.training_unusualness), dtype=np.float64)

    @cached_property
    def _compiled(self) -> _CompiledForest | None:
        forest = self.estimator
        if not isinstance(forest, IsolationForest) or forest._max_features != forest.n_features_in_:
            return None  # only an Isolation Forest over every feature is walked this way
        return _CompiledForest(forest)

    def _unusualness(self, matrix: np.ndarray) -> np.ndarray:
        if self._compiled is not None:
            return self._compiled.unusualness(matrix)
        return np.asarray(-self.estimator.score_samples(matrix), dtype=np.float64)

    def score_matrix(self, matrix: np.ndarray) -> np.ndarray:
        """The Anomaly score of each row of a matrix of model inputs in `INPUT_COLUMNS` order
        (empty ones NaN), in [0, 1]."""
        return self._percentile(self._unusualness(fill_missing(matrix)))

    def score(self, inputs: pd.DataFrame) -> pd.Series:
        """The Anomaly score of each Transaction, in [0, 1], indexed like `inputs`."""
        unusualness = self._unusualness(_matrix(inputs))
        return pd.Series(self._percentile(unusualness), index=inputs.index, name="anomaly_score")

    @cached_property
    def _explainer(self) -> Any:
        if self.method != "isolation_forest":
            raise ValueError(f"only an Isolation Forest can be explained, not {self.method}")
        return shap.TreeExplainer(self.estimator)

    def __getstate__(self) -> dict[str, Any]:
        # The explainer and the flattened trees are rebuilt on first use (under a second)
        # rather than shipped.
        return {k: v for k, v in self.__dict__.items() if k not in ("_explainer", "_compiled")}

    def contributions(self, inputs: pd.DataFrame) -> pd.DataFrame:
        """How much each input moved each Transaction's Anomaly score, indexed like `inputs`.

        SHAP explains an Isolation Forest's average path length: a negative contribution means
        the input isolated the Transaction sooner, so made it more unusual. Unusualness is
        2^(-path length / c) for one constant c, so the path length SHAP starts from has an
        Anomaly score too; each Transaction's rise from that score is shared out in proportion
        to its path length contributions. They add up to the rise exactly.
        """
        matrix = _matrix(inputs)
        return pd.DataFrame(self._contributions(matrix), index=inputs.index, columns=INPUT_COLUMNS)

    def contribution_matrix(self, matrix: np.ndarray) -> np.ndarray:
        """`contributions` of a matrix of model inputs in `INPUT_COLUMNS` order (empty ones NaN),
        as a matrix in the same order."""
        return self._contributions(fill_missing(matrix))

    def _contributions(self, matrix: np.ndarray) -> np.ndarray:
        explainer = self._explainer
        # The additivity check re-predicts every row with a slow Python walk of the trees, and
        # only asserts what TreeSHAP guarantees.
        shares = np.asarray(explainer.shap_values(matrix, check_additivity=False), dtype=np.float64)
        expected = float(np.ravel(explainer.expected_value)[0])
        path = expected + shares.sum(axis=1)
        unusualness = self._unusualness(matrix)
        typical = unusualness ** (expected / path)
        rise = self._percentile(unusualness) - self._percentile(typical)
        moved = path - expected
        safe = np.where(moved == 0, 1.0, moved)
        per_step = np.where(moved == 0, 0.0, rise / safe)
        return np.asarray(shares * per_step[:, None], dtype=np.float64)
