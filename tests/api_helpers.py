"""Calling the /v1 API in tests: settings, request bodies and expected responses."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from txrisk.api import API_KEY_HEADER, ApiSettings
from txrisk.api.auth import SCOPES
from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store import api_keys, create_engine
from txrisk.store.tables import COUNTERPARTY_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


def settings(database: str, bundle: Path | None = None, **options: Any) -> ApiSettings:
    return ApiSettings(
        database_url=database,
        model_bundle=bundle,
        generator_config=ROOT / "config" / "generator.toml",
        feature_config=ROOT / "config" / "features.toml",
        rules_config=ROOT / "config" / "rules.toml",
        **options,
    )


class WallClock:
    """Real time as the API sees it, moved on by hand."""

    def __init__(self, now: datetime = datetime(2030, 6, 1, 9, 0, tzinfo=UTC)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, by: timedelta) -> None:
        self.now += by


def issue_key(
    database: str,
    *scopes: str,
    rate_limit_per_minute: int = 10_000,
    now: datetime | None = None,
) -> str:
    """A new API key with `scopes` (every scope if none are named), issued straight into the
    database as `txrisk api-key` does."""
    engine = create_engine(database)
    with engine.begin() as connection:
        issued = api_keys.issue(
            connection,
            name="test service",
            scopes=scopes or SCOPES,
            rate_limit_per_minute=rate_limit_per_minute,
            created_by="tests",
            now=now or datetime.now(UTC),
        )
    engine.dispose()
    return issued.key


def with_key(key: str) -> dict[str, str]:
    """Request headers presenting an API key."""
    return {API_KEY_HEADER: key}


def signed_in(access: str) -> dict[str, str]:
    """Request headers presenting a person's access token."""
    return {"Authorization": "Bearer " + access}


def body(transaction: Transaction) -> dict[str, Any]:
    """The JSON a caller posts for this Transaction."""
    return {
        "transaction_id": transaction.transaction_id,
        "occurred_at": transaction.occurred_at.isoformat(),
        "transaction_type": transaction.transaction_type,
        "amount": transaction.amount,
        "subject_wallet_id": transaction.subject_wallet_id,
        "direction": transaction.direction,
        "subject_balance_before": transaction.subject_balance_before,
        "channel": transaction.channel,
        "device_id": transaction.device_id,
        "region": transaction.region,
        COUNTERPARTY_COLUMNS[transaction.transaction_type]: transaction.counterparty_id,
    }


def as_json(assessment: RiskAssessment) -> dict[str, Any]:
    return {
        "transaction_id": assessment.transaction_id,
        "risk_score": assessment.risk_score,
        "risk_band": assessment.risk_band.value,
        "reasons": [
            {"code": r.code, "message": r.message, "value": r.value} for r in assessment.reasons
        ],
        "component_scores": assessment.component_scores,
        "model_version": assessment.model_version,
        "degraded": assessment.degraded,
    }
