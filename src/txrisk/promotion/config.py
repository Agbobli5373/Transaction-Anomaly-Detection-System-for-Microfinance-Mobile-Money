"""The Promotion gates' thresholds, from `config/promotion.toml`."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BacktestConfig(_Frozen):
    window_days: int = Field(ge=1)
    min_positives: int = Field(ge=1)
    max_segment_fpr_rise: float = Field(ge=0)
    min_segment_negatives: int = Field(ge=1)


class ShadowConfig(_Frozen):
    min_days: float = Field(gt=0)
    volume_tolerance: float = Field(ge=0, lt=1)
    p95_latency_ms: float = Field(gt=0)


class PromotionConfig(_Frozen):
    backtest: BacktestConfig
    shadow: ShadowConfig


def load_promotion_config(path: Path) -> PromotionConfig:
    with path.open("rb") as f:
        return PromotionConfig.model_validate(tomllib.load(f))
