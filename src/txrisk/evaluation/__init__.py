"""Judging scorers the way a fraud operation would, over time-based splits."""

from txrisk.evaluation.config import EvaluationConfig, load_evaluation_config
from txrisk.evaluation.labels import Split, split_of, training_labels

__all__ = [
    "EvaluationConfig",
    "Split",
    "load_evaluation_config",
    "split_of",
    "training_labels",
]
