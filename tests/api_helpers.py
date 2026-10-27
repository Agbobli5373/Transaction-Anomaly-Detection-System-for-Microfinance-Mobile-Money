"""Calling the /v1 API in tests: settings, request bodies and expected responses."""

from pathlib import Path
from typing import Any

from txrisk.api import ApiSettings
from txrisk.features import Transaction
from txrisk.scoring import RiskAssessment
from txrisk.store.tables import COUNTERPARTY_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


def settings(database: str, bundle: Path | None = None) -> ApiSettings:
    return ApiSettings(
        database_url=database,
        model_bundle=bundle,
        generator_config=ROOT / "config" / "generator.toml",
        feature_config=ROOT / "config" / "features.toml",
        rules_config=ROOT / "config" / "rules.toml",
    )


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
