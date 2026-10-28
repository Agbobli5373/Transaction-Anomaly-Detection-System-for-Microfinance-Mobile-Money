"""What the API runs with, read from `TXRISK_*` environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict

from txrisk.features import FeatureConfig, load_feature_config
from txrisk.generator import load_config
from txrisk.scoring import RulesConfig, load_rules_config
from txrisk.store.redis_history import RedisHistoryCache

_PREFIX = "TXRISK_"


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: str
    # The active model bundle's folder (`models/<version>/` from `txrisk train`); without one
    # the rules alone score.
    model_bundle: Path | None = None
    # The Redis holding the recent-history cache (ADR 0006), e.g. redis://redis:6379/0; without
    # one, history is read from Postgres alone. Every process that stores Transactions must use
    # the same Redis (see store/redis_history.py). No credential belongs in the URL.
    redis_url: str | None = None
    # The market config, for each KYC tier's single-transaction limit.
    generator_config: Path = Path("config/generator.toml")
    feature_config: Path = Path("config/features.toml")
    rules_config: Path = Path("config/rules.toml")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> Self:
        """Settings from `TXRISK_DATABASE_URL`, `TXRISK_MODEL_BUNDLE` and so on; only
        `TXRISK_DATABASE_URL` is required. An empty value counts as unset."""
        values = {
            name: environ[_PREFIX + name.upper()]
            for name in cls.model_fields
            if environ.get(_PREFIX + name.upper())
        }
        return cls.model_validate(values)

    def history_cache(self) -> RedisHistoryCache | None:
        """The recent-history cache, or None when no Redis is configured."""
        return None if self.redis_url is None else RedisHistoryCache.from_url(self.redis_url)

    def features(self) -> FeatureConfig:
        market = load_config(self.generator_config).market
        return load_feature_config(
            self.feature_config,
            kyc_limits={tier.name: tier.single_transaction_limit for tier in market.kyc_tiers},
        )

    def rules(self) -> RulesConfig:
        return load_rules_config(self.rules_config)
