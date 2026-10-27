"""The supervised fraud probability: LightGBM on discovered Fraud labels, calibrated.

The model learns only what the operation knew when it went live: Fraud discoveries made before
the evaluation split starts, with an undiscovered Transaction counting as legitimate only once it
is older than the Label maturity window (ADR 0007). It trains on the train split and is
calibrated on the calibration split, both fully labelled by then (ADR 0008).
"""

from dataclasses import dataclass
from datetime import date

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from txrisk.evaluation import EvaluationConfig, Split, split_of, training_labels
from txrisk.models.inputs import model_inputs


def _raw_probability(classifier: lgb.LGBMClassifier, inputs: pd.DataFrame) -> np.ndarray:
    rows = inputs[classifier.feature_name_].to_numpy(dtype=np.float64)
    return np.asarray(classifier.booster_.predict(rows))


@dataclass(frozen=True)
class CentredIsotonic:
    """Isotonic calibration with each flat step replaced by its centre point.

    Plain isotonic regression is a step function: whole bands of raw probabilities share one
    calibrated value, which throws away the model's ranking inside each step and leaves an
    Alert budget threshold able only to take or leave a step whole. Joining the centres of the
    steps with straight lines keeps the same fit to the labels in each step's middle while
    staying strictly increasing between the lowest and highest centre; beyond those it is flat.
    """

    raw: np.ndarray  # each step's centre, as a raw probability
    probability: np.ndarray  # each step's calibrated probability

    @classmethod
    def fit(cls, raw: np.ndarray, fraud: np.ndarray) -> "CentredIsotonic":
        isotonic = IsotonicRegression(out_of_bounds="clip").fit(raw, fraud)
        # The fitted points are each step's two ends; consecutive points with one value are a step.
        x, y = isotonic.X_thresholds_, isotonic.y_thresholds_
        starts = np.r_[True, np.diff(y) != 0]
        step = np.cumsum(starts) - 1
        centres = np.bincount(step, weights=x) / np.bincount(step)
        return cls(raw=centres, probability=y[starts])

    def predict(self, raw: np.ndarray) -> np.ndarray:
        return np.asarray(np.interp(raw, self.raw, self.probability), dtype=np.float64)


@dataclass(frozen=True)
class SupervisedModel:
    classifier: lgb.LGBMClassifier
    calibrator: CentredIsotonic

    def raw_probability(self, inputs: pd.DataFrame) -> pd.Series:
        """The classifier's own probability, before calibration, indexed like `inputs`.

        Class weighting tilts it towards fraud, so it ranks well but is not to be read as a
        probability. Predicting with the booster on a plain array, in the fitted column order,
        skips the scikit-learn wrapper's checks, which cost far more than the trees for one row.
        """
        fraud = _raw_probability(self.classifier, inputs)
        return pd.Series(fraud, index=inputs.index, name="raw_probability")

    def score(self, inputs: pd.DataFrame) -> pd.Series:
        """The calibrated fraud probability of each row of model inputs, indexed like them."""
        raw = self.raw_probability(inputs)
        return pd.Series(
            self.calibrator.predict(raw.to_numpy()), index=inputs.index, name="fraud_probability"
        )


def _labelled(
    inputs: pd.DataFrame, labels: pd.Series, split: pd.Series, name: Split
) -> tuple[pd.DataFrame, pd.Series]:
    """The inputs and labels of the Transactions in one split that have a label.

    Transactions outside every split have no split and are never in one.
    """
    in_split = (split == name).fillna(False).to_numpy(dtype=bool)
    ids = inputs.index[in_split & inputs.index.isin(labels.index)]
    return inputs.loc[ids], labels.loc[ids]


def train_supervised(
    transactions: pd.DataFrame,
    features: pd.DataFrame,
    discoveries: pd.DataFrame,
    *,
    period_start: date,
    config: EvaluationConfig,
    seed: int,
) -> SupervisedModel:
    """Fit on the train split and calibrate on the calibration split.

    Both use the labels known when the evaluation split starts, the day the model goes live.
    Calibration is centred isotonic, so the probability keeps the classifier's ranking.
    """
    cutoff = pd.Timestamp(period_start, tz="UTC") + pd.Timedelta(
        days=config.splits.evaluation.start
    )
    labels = training_labels(transactions, discoveries, cutoff=cutoff, config=config)
    inputs = model_inputs(transactions, features)
    split = split_of(transactions["occurred_at"], period_start, config)
    x, y = _labelled(inputs, labels, split, Split.TRAIN)
    classifier = lgb.LGBMClassifier(
        class_weight="balanced",
        random_state=seed,
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
    )
    classifier.fit(x, y)
    x, y = _labelled(inputs, labels, split, Split.CALIBRATION)
    calibrator = CentredIsotonic.fit(_raw_probability(classifier, x), y.to_numpy(np.float64))
    return SupervisedModel(classifier, calibrator)
