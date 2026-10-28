"""When retraining may run early, from `config/retraining.toml`."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DriftConfig(_Frozen):
    min_days_between_runs: int = Field(ge=0)


class RetrainingConfig(_Frozen):
    drift: DriftConfig


def load_retraining_config(path: Path) -> RetrainingConfig:
    with path.open("rb") as f:
        return RetrainingConfig.model_validate(tomllib.load(f))
