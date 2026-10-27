"""The parties in the synthetic market: Wallets, Agents, Merchants and Billers."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, time

import numpy as np
import pandas as pd

from txrisk.generator.config import GeneratorConfig


@dataclass(frozen=True)
class Population:
    wallets: pd.DataFrame
    agents: pd.DataFrame
    merchants: pd.DataFrame
    billers: pd.DataFrame


def _weights(values: list[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    normalised: np.ndarray = array / array.sum()
    return normalised


def pseudonymise(msisdn: str, key: bytes) -> str:
    """The Wallet ID for a phone number: an HMAC, unrecoverable without the key."""
    return hmac.new(key, msisdn.encode(), hashlib.sha256).hexdigest()


def _msisdns(n: int, prefixes: list[str], rng: np.random.Generator) -> list[str]:
    """Distinct Ghana phone numbers in international form, e.g. 233241234567."""
    numbers: set[str] = set()
    result: list[str] = []
    while len(result) < n:
        prefix = prefixes[int(rng.integers(len(prefixes)))]
        number = f"233{prefix}{int(rng.integers(10_000_000)):07d}"
        if number not in numbers:
            numbers.add(number)
            result.append(number)
    return result


# Pre-existing Wallets registered at some point in the prior two years.
_PREEXISTING_HISTORY_DAYS = 730


def _registration_times(
    config: GeneratorConfig, start: pd.Timestamp, rng: np.random.Generator
) -> pd.Series:
    n = config.population.wallets
    preexisting = rng.random(n) < config.population.preexisting_wallet_share
    before = -rng.integers(1, _PREEXISTING_HISTORY_DAYS * 86_400, n)
    during = rng.integers(0, config.period.days * 86_400, n)
    return pd.Series(start + pd.to_timedelta(np.where(preexisting, before, during), unit="s"))


def build_population(config: GeneratorConfig, rng: np.random.Generator, key: bytes) -> Population:
    market = config.market
    regions = list(market.regions)
    region_weights = _weights(list(market.regions.values()))
    start = pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))

    n_wallets = config.population.wallets
    tiers = market.kyc_tiers
    # The raw numbers exist only here; just their pseudonyms leave this function.
    msisdns = _msisdns(n_wallets, market.msisdn_prefixes, rng)
    wallets = pd.DataFrame(
        {
            "wallet_id": [pseudonymise(m, key) for m in msisdns],
            "kyc_tier": rng.choice(
                [t.name for t in tiers], n_wallets, p=_weights([t.share for t in tiers])
            ),
            "home_region": rng.choice(regions, n_wallets, p=region_weights),
            "registered_at": _registration_times(config, start, rng),
        }
    )

    n_agents = config.population.agents
    agents = pd.DataFrame(
        {
            "agent_id": [f"A{i:05d}" for i in range(n_agents)],
            "region": rng.choice(regions, n_agents, p=region_weights),
        }
    )

    n_merchants = config.population.merchants
    merchants = pd.DataFrame(
        {
            "merchant_id": [f"M{i:05d}" for i in range(n_merchants)],
            "category": rng.choice(market.merchant_categories, n_merchants),
            "region": rng.choice(regions, n_merchants, p=region_weights),
        }
    )

    billers = pd.DataFrame(
        {
            "biller_id": [f"B{i:03d}" for i in range(len(market.biller_categories))],
            "category": market.biller_categories,
        }
    )
    return Population(wallets=wallets, agents=agents, merchants=merchants, billers=billers)
