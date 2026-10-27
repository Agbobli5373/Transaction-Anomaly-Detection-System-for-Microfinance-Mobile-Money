"""Settings the features are computed with."""

import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from txrisk.features.subject_wallet import DEFAULT_MIN_HISTORY


class FeatureConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Each KYC tier's single-transaction limit in GHS, taken from the market config.
    kyc_limits: dict[str, float]
    # A Cash-out is near-limit when it is at most this share of its Wallet's limit below it.
    near_limit_share: float = Field(default=0.10, gt=0, lt=1)
    # Below this many prior Transactions, history-dependent features are left empty.
    min_history: int = Field(default=DEFAULT_MIN_HISTORY, ge=0)


def load_feature_config(path: Path, kyc_limits: Mapping[str, float]) -> FeatureConfig:
    """Read the feature settings at `path`, completed by the market's KYC tier limits."""
    with path.open("rb") as f:
        settings = tomllib.load(f)
    return FeatureConfig.model_validate({**settings, "kyc_limits": dict(kyc_limits)})
