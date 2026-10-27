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
    REVERSAL_SCAM = "reversal_scam"


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


class ReversalScam(_Scenario):
    bait_amount: Range
    reply_minutes: Range
    return_multiple: Range


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
    reversal_scam: ReversalScam

    def share(self, scenario: FraudScenario) -> float:
        configured: _Scenario = getattr(self, scenario.value)
        return configured.share


class Discovery(_Frozen):
    delay_days: Range
    chargeback_share: float = Field(ge=0, le=1)
    probability: dict[FraudScenario, float]

    @model_validator(mode="after")
    def _every_scenario_has_a_probability(self) -> "Discovery":
        missing = set(FraudScenario) - set(self.probability)
        if missing:
            raise ValueError(f"discovery missing probabilities for: {sorted(missing)}")
        return self


class Drift(_Frozen):
    day: int = Field(ge=0)
    new_scenario: FraudScenario
    shifted_scenario: FraudScenario
    sim_swap_takeover: SimSwapTakeover | None = None

    @model_validator(mode="before")
    @classmethod
    def _shifted_parameters_inherit_share(cls, value: object) -> object:
        # The shifted block lists only parameters; its share is irrelevant once drifted.
        if isinstance(value, dict) and isinstance(value.get("sim_swap_takeover"), dict):
            value = {**value, "sim_swap_takeover": {"share": 1.0, **value["sim_swap_takeover"]}}
        return value


class Fraud(_Frozen):
    rate: float = Field(ge=0, lt=1)
    legitimate_sim_swap_share: float = Field(ge=0, le=1)
    victim_min_median_balance: float = Field(ge=0)
    scenarios: FraudScenarios
    discovery: Discovery
    drift: Drift


class FestiveSeason(_Frozen):
    name: str
    start: date
    end: date
    activity_multiple: float = Field(gt=0)
    amount_multiple: float = Field(gt=0)


class Benign(_Frozen):
    salaried_share: float = Field(ge=0, le=1)
    pay_day: Range
    pay_amount: Range
    large_payment_share: float = Field(ge=0, le=1)
    large_payment_multiple: Range
    travel_share: float = Field(ge=0, le=1)
    travel_days: Range
    new_phone_share: float = Field(ge=0, le=1)
    new_phone_app_share: float = Field(ge=0, le=1)
    festive: list[FestiveSeason]


class GeneratorConfig(_Frozen):
    period: Period
    population: Population
    market: Market
    behaviour: Behaviour
    fraud: Fraud
    benign: Benign


def load_config(path: Path) -> GeneratorConfig:
    with path.open("rb") as f:
        return GeneratorConfig.model_validate(tomllib.load(f))
