"""What the API runs with, read from `TXRISK_*` environment variables."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from txrisk.features import FeatureConfig, load_feature_config
from txrisk.generator import load_config
from txrisk.scoring import RulesConfig, load_rules_config

_PREFIX = "TXRISK_"


class ApiSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    database_url: str
    # The active model bundle's folder (`models/<version>/` from `txrisk train`); without one
    # the rules alone score.
    model_bundle: Path | None = None
    # The market config, for each KYC tier's single-transaction limit.
    generator_config: Path = Path("config/generator.toml")
    feature_config: Path = Path("config/features.toml")
    rules_config: Path = Path("config/rules.toml")

    # Sign-in for people, through Keycloak. The issuer is the realm URL as tokens name it
    # (http://localhost:8080/realms/txrisk in compose); without one only API keys are accepted.
    oidc_issuer: str | None = None
    # The audience the realm's clients put in their access tokens for this API.
    oidc_audience: str = "txrisk-api"
    # Where to fetch the realm's signing keys, when the API reaches Keycloak by another address
    # than the issuer's (in compose, http://keycloak:8080/...). Defaults to the issuer's.
    oidc_jwks_url: str | None = None
    # The realm clients whose tokens the API accepts, comma-separated in
    # TXRISK_OIDC_ALLOWED_CLIENTS: the token's `azp`. Production allows the workbench alone;
    # compose adds txrisk-demo-cli, the dev-only command-line sign-in.
    oidc_allowed_clients: tuple[str, ...] = ("txrisk-workbench",)

    # Browser origins allowed to call the API, comma-separated in TXRISK_CORS_ORIGINS; none by
    # default. A wildcard is refused: the allow-list is strict.
    cors_origins: tuple[str, ...] = ()
    # Larger request bodies are refused with 413. A Transaction is well under 1 KiB.
    max_request_bytes: int = Field(default=64 * 1024, gt=0)
    # Requests a minute for an API key issued without its own limit.
    api_key_rate_limit: int = Field(default=600, gt=0)
    # How long a rotated API key stays valid alongside its replacement, unless the rotation says.
    api_key_rotation_overlap_minutes: int = Field(default=24 * 60, ge=0)

    @field_validator("cors_origins", "oidc_allowed_clients", mode="before")
    @classmethod
    def _comma_separated(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError("list each allowed origin; a wildcard is not allowed")
        return value

    @property
    def jwks_url(self) -> str | None:
        if self.oidc_jwks_url is not None:
            return self.oidc_jwks_url
        if self.oidc_issuer is None:
            return None
        return self.oidc_issuer.rstrip("/") + "/protocol/openid-connect/certs"

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

    def features(self) -> FeatureConfig:
        market = load_config(self.generator_config).market
        return load_feature_config(
            self.feature_config,
            kyc_limits={tier.name: tier.single_transaction_limit for tier in market.kyc_tiers},
        )

    def rules(self) -> RulesConfig:
        return load_rules_config(self.rules_config)
