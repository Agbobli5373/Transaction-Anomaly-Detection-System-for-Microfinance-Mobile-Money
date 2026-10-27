"""Benign anomalies: unusual but legitimate behaviour that should not be mistaken for fraud."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum

import numpy as np
import pandas as pd

from txrisk.generator.config import GeneratorConfig, Range
from txrisk.generator.market import (
    CASH_IN,
    TYPES,
    local_pick,
    members_by_region,
    region_codes,
    region_weights,
)
from txrisk.generator.population import Population


class BenignAnomaly(StrEnum):
    SALARY_INFLOW = "salary_inflow"
    LARGE_PAYMENT = "large_payment"
    TRAVEL = "travel"
    NEW_PHONE = "new_phone"


BENIGN = list(BenignAnomaly)
_PAYMENT = TYPES.index(next(t for t in TYPES if t.value == "payment"))


def _uniform(rng: np.random.Generator, value: Range, size: int) -> np.ndarray:
    return rng.uniform(value.low, value.high, size)


def _candidates(rows: pd.DataFrame | dict[str, object]) -> pd.DataFrame:
    """Fill in the candidate columns a benign Transaction does not set itself."""
    frame = pd.DataFrame(rows)
    frame["counterparty_wallet"] = 0
    frame["merchant_id"] = frame.get("merchant_id", 0)
    frame["agent_id"] = frame.get("agent_id", 0)
    frame["biller_id"] = 0
    frame["scenario"] = -1
    frame["instance"] = -1
    frame["device"] = None
    frame["channel"] = None
    frame["drain_share"] = np.nan
    frame["require_exact"] = False
    return frame


def _pay_days(config: GeneratorConfig, day_of_month: int) -> list[date]:
    """Each date in the period that falls on this day of the month."""
    first = config.period.start
    days = (first + timedelta(days=d) for d in range(config.period.days))
    return [d for d in days if d.day == day_of_month]


def salary_inflows(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> pd.DataFrame:
    params = config.benign
    n_wallets = len(population.wallets)
    salaried = rng.choice(n_wallets, round(params.salaried_share * n_wallets), replace=False)
    pay_day = rng.integers(round(params.pay_day.low), round(params.pay_day.high) + 1, len(salaried))
    pay = _uniform(rng, params.pay_amount, len(salaried))
    regions = list(config.market.regions)
    home = region_codes(population.wallets["home_region"], regions)
    agents = members_by_region(population, regions)["agent_id"]

    rows: list[dict[str, object]] = []
    for wallet, day, amount in zip(salaried, pay_day, pay, strict=True):
        for paid_on in _pay_days(config, int(day)):
            # Paid during working hours, varying a little month to month.
            seconds = int(rng.integers(9 * 3600, 17 * 3600))
            rows.append(
                {
                    "occurred_at": pd.Timestamp(datetime.combine(paid_on, time(), tzinfo=UTC))
                    + pd.Timedelta(seconds=seconds),
                    "subject": int(wallet),
                    "type_index": CASH_IN,
                    "intended_pesewas": round(
                        np.clip(
                            amount * rng.uniform(0.9, 1.1),
                            params.pay_amount.low,
                            params.pay_amount.high,
                        )
                        * 100
                    ),
                    "location": int(home[wallet]),
                    "benign": BENIGN.index(BenignAnomaly.SALARY_INFLOW),
                }
            )
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["agent_id"] = local_pick(frame["location"].to_numpy(), agents, rng)
    return _candidates(frame)


def large_payments(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> pd.DataFrame:
    params = config.benign
    n_wallets = len(population.wallets)
    payers = rng.choice(n_wallets, round(params.large_payment_share * n_wallets), replace=False)
    start = pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))
    paid_at = start + pd.to_timedelta(
        rng.uniform(1, config.period.days, len(payers)) * 86_400, unit="s"
    )
    median = config.behaviour.transaction_types[TYPES[_PAYMENT]].median_amount
    amount = np.round(_uniform(rng, params.large_payment_multiple, len(payers)) * median * 100)
    regions = list(config.market.regions)
    home = region_codes(population.wallets["home_region"], regions)[payers]
    local = members_by_region(population, regions)
    # The holder tops up first so the money is there, then pays.
    lead = pd.to_timedelta(rng.uniform(10, 120, len(payers)), unit="m")
    top_ups = {
        "occurred_at": paid_at - lead,
        "subject": payers,
        "type_index": CASH_IN,
        "intended_pesewas": amount.astype(np.int64),
        "location": home,
        "agent_id": local_pick(home, local["agent_id"], rng),
        "benign": -1,
    }
    purchases = {
        "occurred_at": paid_at,
        "subject": payers,
        "type_index": _PAYMENT,
        "intended_pesewas": amount.astype(np.int64),
        "location": home,
        "merchant_id": local_pick(home, local["merchant_id"], rng),
        "benign": BENIGN.index(BenignAnomaly.LARGE_PAYMENT),
    }
    frame = pd.concat([_candidates(top_ups), _candidates(purchases)], ignore_index=True)
    frame["require_exact"] = frame["type_index"] == _PAYMENT
    return frame


def benign_candidates(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> pd.DataFrame:
    """Legitimate Transactions that are unusual for the Wallet making them."""
    parts = [salary_inflows(config, population, rng), large_payments(config, population, rng)]
    return pd.concat([p for p in parts if len(p)], ignore_index=True)


@dataclass(frozen=True)
class Trips:
    """Per Wallet: when it is away (naive UTC; NaT if never) and in which region."""

    starts: np.ndarray
    ends: np.ndarray
    region: np.ndarray

    def away(self, subject: np.ndarray, occurred: np.ndarray) -> np.ndarray:
        on_trip: np.ndarray = (occurred >= self.starts[subject]) & (occurred < self.ends[subject])
        return on_trip


def plan_trips(config: GeneratorConfig, population: Population, rng: np.random.Generator) -> Trips:
    params = config.benign
    n_wallets = len(population.wallets)
    regions = list(config.market.regions)
    home = region_codes(population.wallets["home_region"], regions)
    local = members_by_region(population, regions)
    # Trips go where the traveller can find Agents and Merchants.
    served = np.asarray(
        [
            local["agent_id"][r].size > 0 and local["merchant_id"][r].size > 0
            for r in range(len(regions))
        ]
    )
    weights = region_weights(config)

    starts = np.full(n_wallets, np.datetime64("NaT", "s"), dtype="datetime64[s]")
    ends = starts.copy()
    region = np.full(n_wallets, -1)
    start = np.datetime64(config.period.start, "s")
    for wallet in rng.choice(n_wallets, round(params.travel_share * n_wallets), replace=False):
        options = served & (np.arange(len(regions)) != home[wallet])
        if not options.any():
            continue
        days = rng.uniform(params.travel_days.low, params.travel_days.high)
        leave = rng.uniform(0, max(config.period.days - days, 0))
        starts[wallet] = start + np.timedelta64(round(leave * 86_400), "s")
        ends[wallet] = starts[wallet] + np.timedelta64(round(days * 86_400), "s")
        region[wallet] = rng.choice(
            np.flatnonzero(options), p=weights[options] / weights[options].sum()
        )
    return Trips(starts=starts, ends=ends, region=region)


@dataclass(frozen=True)
class NewPhones:
    """Per Wallet: when it moves to a new phone (naive UTC; NaT if never), and to what."""

    switch_at: np.ndarray
    device: np.ndarray
    to_app: np.ndarray


def plan_new_phones(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> NewPhones:
    params = config.benign
    n_wallets = len(population.wallets)
    start = np.datetime64(config.period.start, "s")
    switch_at = np.full(n_wallets, np.datetime64("NaT", "s"), dtype="datetime64[s]")
    device = np.full(n_wallets, None, dtype=object)
    to_app = np.zeros(n_wallets, dtype=bool)
    registered = population.wallets["registered_at"].dt.tz_convert(None).to_numpy()
    existing = np.flatnonzero(registered < start)
    count = min(round(params.new_phone_share * n_wallets), existing.size)
    for wallet in rng.choice(existing, count, replace=False):
        switch_at[wallet] = start + np.timedelta64(
            round(rng.uniform(1, config.period.days) * 86_400), "s"
        )
        device[wallet] = f"D{int(rng.integers(0, 2**63 - 1)):016x}"
        to_app[wallet] = rng.random() < params.new_phone_app_share
    return NewPhones(switch_at=switch_at, device=device, to_app=to_app)


def festive_multiples(config: GeneratorConfig, day: date) -> tuple[float, float]:
    """How much busier, and how much larger, Transactions are on this day."""
    for season in config.benign.festive:
        if season.start <= day <= season.end:
            return season.activity_multiple, season.amount_multiple
    return 1.0, 1.0
