"""How monitoring judges the model, from `config/monitoring.toml`."""

import tomllib
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PsiConfig(_Frozen):
    warn: float = Field(gt=0)
    action: float = Field(gt=0)
    min_rows: int = Field(ge=1)
    # Inputs that grow with time since the data began (a Wallet's history, a device's age), so
    # their PSI measures the calendar and not drift: neither is measured.
    ignore: tuple[str, ...] = ()
    # Drift among the Alerts: the inputs of High Transactions over this many trailing days,
    # needing at least this many of them.
    alerted_window_days: int = Field(ge=1)
    alerted_min_rows: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.warn >= self.action:
            raise ValueError("the PSI warning threshold must be below the action threshold")
        return self


class BaselineConfig(_Frozen):
    bins: int = Field(ge=2, le=50)


class AlertVolumeConfig(_Frozen):
    min_share_of_budget: float = Field(ge=0)
    max_share_of_budget: float = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_share_of_budget >= self.max_share_of_budget:
            raise ValueError("the Alert volume band must have its minimum below its maximum")
        return self


class LabelsConfig(_Frozen):
    window_days: int = Field(ge=1)
    min_positives: int = Field(ge=1)
    max_relative_drop: float = Field(gt=0, lt=1)
    reference_days: int = Field(ge=1)


class DiscoveriesConfig(_Frozen):
    window_days: int = Field(ge=1)


class MonitoringConfig(_Frozen):
    psi: PsiConfig
    baseline: BaselineConfig
    alert_volume: AlertVolumeConfig
    labels: LabelsConfig
    discoveries: DiscoveriesConfig


def load_monitoring_config(path: Path) -> MonitoringConfig:
    with path.open("rb") as f:
        return MonitoringConfig.model_validate(tomllib.load(f))
