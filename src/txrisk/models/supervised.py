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


@dataclass(frozen=True)
class SupervisedModel:
    classifier: lgb.LGBMClassifier
    calibrator: IsotonicRegression

    def raw_probability(self, inputs: pd.DataFrame) -> pd.Series:
        """The classifier's own probability, before calibration, indexed like `inputs`.

        Class weighting tilts it towards fraud, so it ranks well but is not to be read as a
        probability. Predicting with the booster on a plain array, in the fitted column order,
        skips the scikit-learn wrapper's checks, which cost far more than the trees for one row.
        """
        rows = inputs[self.classifier.feature_name_].to_numpy(dtype=np.float64)
        fraud = np.asarray(self.classifier.booster_.predict(rows))
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
    """The inputs and labels of the Transactions in one split that have a label."""
    ids = inputs.index[(split == name).to_numpy() & inputs.index.isin(labels.index)]
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
    model = SupervisedModel(classifier, IsotonicRegression(out_of_bounds="clip"))
    x, y = _labelled(inputs, labels, split, Split.CALIBRATION)
    model.calibrator.fit(model.raw_probability(x).to_numpy(), y.to_numpy())
    return model
