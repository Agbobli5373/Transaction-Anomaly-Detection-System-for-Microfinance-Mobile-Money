"""How the period is split over time and what the evaluation measures, loaded from TOML."""

import itertools
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Days(_Frozen):
    """Days from the period start: includes `start`, excludes `end`."""

    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            start, end = value
            return {"start": start, "end": end}
        return value


class Splits(_Frozen):
    train: Days
    calibration: Days
    thresholds: Days
    evaluation: Days

    @model_validator(mode="after")
    def _back_to_back(self) -> "Splits":
        ordered = [self.train, self.calibration, self.thresholds, self.evaluation]
        if any(a.end != b.start for a, b in itertools.pairwise(ordered)):
            raise ValueError("splits must follow each other without gaps or overlaps")
        return self


class EvaluationConfig(_Frozen):
    splits: Splits
    label_maturity_days: int = Field(ge=0)
    alert_budget_per_day: int = Field(gt=0)
    # How fast the High threshold rises with the market spend index (0 keeps it fixed), and the
    # largest budget reduction it may reach: see ADR 0011.
    surge_elasticity: float = Field(default=0.0, ge=0)
    surge_max_divisor: float = Field(default=3.0, gt=1)
    wallet_age_band_edges: list[int]


def load_evaluation_config(path: Path) -> EvaluationConfig:
    with path.open("rb") as f:
        return EvaluationConfig.model_validate(tomllib.load(f))
