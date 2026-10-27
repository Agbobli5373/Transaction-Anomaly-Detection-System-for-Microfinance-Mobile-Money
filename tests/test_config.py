import tomllib
from pathlib import Path

import pytest
from conftest import DEFAULT_CONFIG
from pydantic import ValidationError

from txrisk.generator import GeneratorConfig, TransactionType, load_config


def test_default_config_describes_the_full_ghana_market(default_config: GeneratorConfig):
    assert default_config.period.days == 120
    assert default_config.population.wallets == 10_000
    assert default_config.market.currency == "GHS"
    assert len(default_config.market.regions) == 16
    assert set(default_config.behaviour.transaction_types) == set(TransactionType)


def test_a_config_missing_a_transaction_type_is_rejected(tmp_path: Path):
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    without_payment = text.replace("[behaviour.transaction_types.payment]", "[unused.payment]")
    path = tmp_path / "broken.toml"
    path.write_text(without_payment, encoding="utf-8")

    assert "payment" not in tomllib.loads(without_payment)["behaviour"]["transaction_types"]
    with pytest.raises(ValidationError, match="payment"):
        load_config(path)
