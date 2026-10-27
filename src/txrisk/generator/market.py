"""Shared facts about the market that everyday activity and fraud both draw on."""

import numpy as np
import pandas as pd

from txrisk.generator.config import GeneratorConfig, TransactionType
from txrisk.generator.population import Population

TYPES = list(TransactionType)
CASH_IN = TYPES.index(TransactionType.CASH_IN)
CASH_OUT = TYPES.index(TransactionType.CASH_OUT)
TRANSFER = TYPES.index(TransactionType.TRANSFER)
COUNTERPARTY_COLUMN = {
    TransactionType.CASH_IN: "agent_id",
    TransactionType.CASH_OUT: "agent_id",
    TransactionType.TRANSFER: "counterparty_wallet_id",
    TransactionType.PAYMENT: "merchant_id",
    TransactionType.BILL_PAYMENT: "biller_id",
}


def contacts(n_wallets: int, per_wallet: int, rng: np.random.Generator) -> np.ndarray:
    """Each Wallet's circle of other Wallets it Transfers to, never including itself."""
    picks = rng.integers(0, n_wallets - 1, (n_wallets, per_wallet))
    circle: np.ndarray = picks + (picks >= np.arange(n_wallets)[:, None])
    return circle


def region_codes(values: pd.Series, regions: list[str]) -> np.ndarray:
    return np.asarray(pd.Categorical(values, categories=regions).codes)


def region_weights(config: GeneratorConfig) -> np.ndarray:
    weights = np.asarray(list(config.market.regions.values()), dtype=float)
    normalised: np.ndarray = weights / weights.sum()
    return normalised


def members_by_region(population: Population, regions: list[str]) -> dict[str, list[np.ndarray]]:
    """For Agents and Merchants: the indexes of those located in each region."""
    tables = {"agent_id": population.agents, "merchant_id": population.merchants}
    result: dict[str, list[np.ndarray]] = {}
    for column, table in tables.items():
        codes = region_codes(table["region"], regions)
        result[column] = [np.flatnonzero(codes == r) for r in range(len(regions))]
    return result


def local_pick(
    location: np.ndarray, members: list[np.ndarray], rng: np.random.Generator
) -> np.ndarray:
    """A Counterparty in the Transaction's region, or anywhere if that region has none."""
    total = sum(m.size for m in members)
    picks = rng.integers(0, total, len(location))
    for region, here_members in enumerate(members):
        here = location == region
        if here_members.size and here.any():
            picks[here] = here_members[rng.integers(0, here_members.size, int(here.sum()))]
    return picks
