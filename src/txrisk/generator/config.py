"""Generator configuration: the synthetic market and population, loaded from TOML."""

import tomllib
from datetime import date
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransactionType(StrEnum):
    CASH_IN = "cash_in"
    CASH_OUT = "cash_out"
    TRANSFER = "transfer"
    PAYMENT = "payment"
    BILL_PAYMENT = "bill_payment"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Period(_Frozen):
    start: date
    days: int = Field(gt=0)


class Population(_Frozen):
    wallets: int = Field(gt=1)
    agents: int = Field(gt=0)
    merchants: int = Field(gt=0)
    preexisting_wallet_share: float = Field(ge=0, le=1)
    contacts_per_wallet: int = Field(gt=0)


class KycTier(_Frozen):
    name: str
    share: float = Field(gt=0)
    single_transaction_limit: int = Field(gt=0)
    max_balance: int = Field(gt=0)


class Market(_Frozen):
    currency: str
    msisdn_prefixes: list[str]
    merchant_categories: list[str]
    biller_categories: list[str]
    regions: dict[str, float]
    kyc_tiers: list[KycTier]


class TransactionTypeBehaviour(_Frozen):
    share: float = Field(gt=0)
    median_amount: float = Field(gt=0)


class Behaviour(_Frozen):
    mean_transactions_per_day: float = Field(gt=0)
    median_opening_balance: float = Field(ge=0)
    weekend_activity_factor: float = Field(gt=0)
    usd_channel_share: float = Field(ge=0, le=1)
    home_region_share: float = Field(ge=0, le=1)
    hourly_activity: list[float] = Field(min_length=24, max_length=24)
    transaction_types: dict[TransactionType, TransactionTypeBehaviour]

    @model_validator(mode="after")
    def _every_transaction_type_configured(self) -> "Behaviour":
        missing = set(TransactionType) - set(self.transaction_types)
        if missing:
            raise ValueError(f"behaviour missing transaction types: {sorted(missing)}")
        return self


class FraudScenario(StrEnum):
    SIM_SWAP_TAKEOVER = "sim_swap_takeover"
    ACCOUNT_TAKEOVER_DRAIN = "account_takeover_drain"
    MULE_FAN_IN = "mule_fan_in"
    AGENT_STRUCTURING = "agent_structuring"
    SOCIAL_ENGINEERING_PUSH = "social_engineering_push"


class Range(_Frozen):
    """An inclusive [min, max] range an instance draws a parameter from."""

    low: float
    high: float

    @model_validator(mode="before")
    @classmethod
    def _from_pair(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            low, high = value
            return {"low": low, "high": high}
        return value

    @model_validator(mode="after")
    def _ordered(self) -> "Range":
        if self.low > self.high:
            raise ValueError(f"range minimum {self.low} exceeds maximum {self.high}")
        return self

    @property
    def mean(self) -> float:
        return (self.low + self.high) / 2


class _Scenario(_Frozen):
    share: float = Field(gt=0)


class SimSwapTakeover(_Scenario):
    delay_hours: Range
    transactions: Range
    drain_share: Range


class AccountTakeoverDrain(_Scenario):
    burst_minutes: Range
    transactions: Range
    drain_share: Range


class MuleFanIn(_Scenario):
    senders: Range
    window_hours: Range
    sent_amount: Range
    cash_out_delay_hours: Range
    cash_outs: Range


class AgentStructuring(_Scenario):
    cash_outs: Range
    window_hours: Range
    limit_share: Range


class SocialEngineeringPush(_Scenario):
    top_up_multiple: Range
    top_up_lead_minutes: Range
    drain_share: Range


class FraudScenarios(_Frozen):
    sim_swap_takeover: SimSwapTakeover
    account_takeover_drain: AccountTakeoverDrain
    mule_fan_in: MuleFanIn
    agent_structuring: AgentStructuring
    social_engineering_push: SocialEngineeringPush

    def share(self, scenario: FraudScenario) -> float:
        configured: _Scenario = getattr(self, scenario.value)
        return configured.share


class Fraud(_Frozen):
    rate: float = Field(ge=0, lt=1)
    legitimate_sim_swap_share: float = Field(ge=0, le=1)
    victim_min_median_balance: float = Field(ge=0)
    scenarios: FraudScenarios


class GeneratorConfig(_Frozen):
    period: Period
    population: Population
    market: Market
    behaviour: Behaviour
    fraud: Fraud


def load_config(path: Path) -> GeneratorConfig:
    with path.open("rb") as f:
        return GeneratorConfig.model_validate(tomllib.load(f))
