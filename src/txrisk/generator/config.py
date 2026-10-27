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


class GeneratorConfig(_Frozen):
    period: Period
    population: Population
    market: Market
    behaviour: Behaviour


def load_config(path: Path) -> GeneratorConfig:
    with path.open("rb") as f:
        return GeneratorConfig.model_validate(tomllib.load(f))
