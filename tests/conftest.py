from pathlib import Path

import pytest

from txrisk.generator import GeneratorConfig, load_config

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "generator.toml"
KEY = b"test-pseudonym-key"


@pytest.fixture(scope="session")
def default_config() -> GeneratorConfig:
    return load_config(DEFAULT_CONFIG)


@pytest.fixture(scope="session")
def small_config(default_config: GeneratorConfig) -> GeneratorConfig:
    """The default market at a size that generates in well under a second."""
    return default_config.model_copy(
        update={
            "period": default_config.period.model_copy(update={"days": 21}),
            "population": default_config.population.model_copy(
                update={"wallets": 300, "agents": 12, "merchants": 10}
            ),
        }
    )
