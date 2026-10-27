"""The stacker: one Risk score from the rules, the Anomaly score and the supervised probability.

A small logistic regression learns how much each component should count. Each component's weight
is a single readable coefficient, which is the point of stacking with something this simple.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

STACK_COLUMNS = ["rules", "anomaly", "supervised_log_odds"]
# Probabilities are kept this far from 0 and 1 so their log-odds stay finite.
_EDGE = 1e-6


def stack_components(
    *, rules_score: pd.Series, anomaly_score: pd.Series, raw_probability: pd.Series
) -> pd.DataFrame:
    """The components on the scales the stacker learns from.

    The rules score and the Anomaly score are already on 0-1. The supervised model's raw
    probability goes in as log-odds: calibration is monotone and fitted on the same days as the
    stacker, so the stacker learns its own scaling from the raw probability instead.
    """
    clipped = raw_probability.clip(_EDGE, 1 - _EDGE).to_numpy(dtype=np.float64)
    return pd.DataFrame(
        {
            "rules": rules_score.to_numpy(dtype=np.float64),
            "anomaly": anomaly_score.to_numpy(dtype=np.float64),
            "supervised_log_odds": np.log(clipped / (1 - clipped)),
        },
        index=rules_score.index,
    )


@dataclass(frozen=True)
class Stacker:
    weights: dict[str, float]
    intercept: float

    @classmethod
    def fit(cls, components: pd.DataFrame, fraud: pd.Series) -> "Stacker":
        regression = LogisticRegression(max_iter=1000)
        regression.fit(components[STACK_COLUMNS].to_numpy(), fraud.to_numpy(dtype=int))
        weights = dict(zip(STACK_COLUMNS, map(float, regression.coef_[0]), strict=True))
        return cls(weights=weights, intercept=float(regression.intercept_[0]))

    def score(self, components: pd.DataFrame) -> pd.Series:
        """The Risk score, 0-100, before any Critical rule floor."""
        log_odds = self.intercept + sum(
            self.weights[c] * components[c].to_numpy(dtype=np.float64) for c in STACK_COLUMNS
        )
        return pd.Series(100 / (1 + np.exp(-log_odds)), index=components.index, name="risk_score")
