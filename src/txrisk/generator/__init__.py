"""Seeded generator of a synthetic Ghana-style mobile money dataset."""

import numpy as np

from txrisk.generator.activity import simulate
from txrisk.generator.config import FraudScenario, GeneratorConfig, TransactionType, load_config
from txrisk.generator.dataset import Dataset, write_dataset
from txrisk.generator.population import build_population

__all__ = [
    "Dataset",
    "FraudScenario",
    "GeneratorConfig",
    "TransactionType",
    "generate",
    "load_config",
    "write_dataset",
]


def generate(config: GeneratorConfig, seed: int, key: bytes) -> Dataset:
    """Generate the dataset; the same config, seed and key always give the same result."""
    rng = np.random.default_rng(seed)
    population = build_population(config, rng, key)
    activity = simulate(config, population, rng)
    return Dataset(
        wallets=population.wallets,
        agents=population.agents,
        merchants=population.merchants,
        billers=population.billers,
        transactions=activity.transactions,
        sim_swaps=activity.sim_swaps,
        ground_truth=activity.ground_truth,
    )
