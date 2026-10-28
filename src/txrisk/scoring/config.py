"""Rule thresholds and how Rule hits combine, loaded from TOML."""

import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Rule(_Frozen):
    severity: Severity


class SimSwapDrain(_Rule):
    within_hours: float = Field(gt=0)
    min_share_of_balance: float = Field(gt=0, le=1)


class NewDeviceBurst(_Rule):
    device_new_within_minutes: float = Field(gt=0)
    min_new_counterparty_transfers: int = Field(gt=0)


class MuleCashOut(_Rule):
    min_distinct_senders_24h: int = Field(gt=0)


class AgentStructuring(_Rule):
    min_share_of_limit: float = Field(gt=0, le=1)
    min_near_limit_cash_outs_24h: int = Field(gt=0)


class NearLimitCashIn(_Rule):
    min_share_of_limit: float = Field(gt=0, le=1)
    above_largest_multiple: float = Field(gt=0)
    # A younger Wallet has no settled size of Cash-in yet, so a first large one says little.
    min_wallet_age_days: float = Field(ge=0)


class AboveMedian(_Rule):
    above_median_multiple: float = Field(gt=0)
    # A younger Wallet is still settling into its usual amounts and Counterparties, so a large
    # amount against its short history says little.
    min_wallet_age_days: float = Field(ge=0)


class NearKycLimit(_Rule):
    min_share_of_limit: float = Field(gt=0, le=1)


class OddHour(_Rule):
    night_ends_at_hour: int = Field(gt=0, le=24)
    max_night_share: float = Field(ge=0, le=1)


class Rules(_Frozen):
    SIM_SWAP_DRAIN: SimSwapDrain
    NEW_DEVICE_BURST: NewDeviceBurst
    MULE_CASHOUT: MuleCashOut
    AGENT_STRUCTURING: AgentStructuring
    NEAR_LIMIT_CASH_IN: NearLimitCashIn
    RETURN_TO_NEW_SENDER: _Rule
    FIRST_TIME_LARGE_TRANSFER: AboveMedian
    AMOUNT_VS_MEDIAN: AboveMedian
    NEAR_KYC_LIMIT: NearKycLimit
    NEW_REGION: _Rule
    ODD_HOUR: OddHour
    # Codes of rules that are switched off because their inputs do not exist in the data being
    # scored (the PaySim benchmark has no devices or SIM swaps). Empty in every real deployment.
    disabled: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _known_codes(self) -> "Rules":
        unknown = self.disabled - (set(type(self).model_fields) - {"disabled"})
        if unknown:
            raise ValueError(f"cannot disable unknown rules: {sorted(unknown)}")
        return self


class Bands(_Frozen):
    medium: float = Field(gt=0, le=100)
    high: float = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> "Bands":
        if self.medium >= self.high:
            raise ValueError("the Medium band must start below the High band")
        return self


class RulesConfig(_Frozen):
    max_reasons: int = Field(gt=0)
    critical_floor: float = Field(ge=0, le=100)
    severity_weights: dict[Severity, float]
    bands: Bands
    rules: Rules

    @model_validator(mode="after")
    def _consistent(self) -> "RulesConfig":
        missing = set(Severity) - set(self.severity_weights)
        if missing:
            raise ValueError(f"severity_weights missing: {sorted(missing)}")
        if self.critical_floor < self.bands.high:
            raise ValueError("critical_floor must be inside the High band")
        return self


def load_rules_config(path: Path) -> RulesConfig:
    with path.open("rb") as f:
        return RulesConfig.model_validate(tomllib.load(f))
