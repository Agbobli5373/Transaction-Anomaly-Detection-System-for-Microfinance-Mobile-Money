"""The model card: written with every bundle that is trained, saved beside it, and readable by a
model risk reviewer without the code."""

import dataclasses
import json
from pathlib import Path

import pytest
from market_helpers import Market, build_market, train

from txrisk.generator import GeneratorConfig
from txrisk.models.bundle import ModelBundle
from txrisk.models.card import CARD_FILE, MARKDOWN_FILE, load_card, render_markdown


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return train(market)


def test_a_trained_bundle_carries_a_card_with_intended_use_data_metrics_and_limitations(
    bundle: ModelBundle,
):
    assert bundle.card is not None

    card = bundle.card

    assert card["version"] == bundle.version
    assert any("Advisory only" in line for line in card["intended_use"])
    assert card["limitations"]
    data = card["data"]
    assert data["fitted_on_days"] == bundle.fitted_on
    assert data["fraud_labels_known"] > 0
    assert data["label_maturity_days"] == 3
    assert set(data["transactions_by_split"]) == {
        "train",
        "calibration",
        "thresholds",
        "evaluation",
    }
    assert card["model"]["thresholds"] == {
        "medium": bundle.thresholds.medium,
        "high": bundle.thresholds.high,
    }


def test_the_card_gives_metrics_for_each_segment_the_evaluation_uses(bundle: ModelBundle):
    assert bundle.card is not None

    metrics = bundle.card["metrics"]

    assert set(metrics["by_segment"]) == {"region", "kyc_tier", "channel", "wallet_age_band"}
    assert metrics["overall"]["transactions"] > 0
    assert metrics["overall"]["fraud"] > 0
    for groups in metrics["by_segment"].values():
        assert groups
        for row in groups.values():
            assert {"transactions", "fraud", "precision", "recall", "false_positive_rate"} <= set(
                row
            )
    # Each segment's Transactions add up to the whole.
    for groups in metrics["by_segment"].values():
        assert sum(r["transactions"] for r in groups.values()) == metrics["overall"]["transactions"]


def test_saving_a_bundle_writes_the_card_as_data_and_as_a_page(bundle: ModelBundle, tmp_path: Path):
    bundle.save(tmp_path)

    saved = json.loads((tmp_path / CARD_FILE).read_text(encoding="utf-8"))
    page = (tmp_path / MARKDOWN_FILE).read_text(encoding="utf-8")

    assert saved == bundle.card
    assert load_card(tmp_path) == saved
    for heading in ("Intended use", "Data", "Metrics by segment", "Limitations"):
        assert f"## {heading}" in page
    assert bundle.version in page
    assert "### region" in page


def test_a_bundle_saved_before_cards_existed_still_has_one_that_says_what_is_missing(
    bundle: ModelBundle, tmp_path: Path
):
    old = dataclasses.replace(bundle, card=None)
    old.save(tmp_path)
    (tmp_path / CARD_FILE).unlink()  # as if it was never written

    card = load_card(tmp_path)

    assert card["version"] == bundle.version
    assert card["metrics"] == {"recorded": False}
    page = render_markdown(card)
    assert "Not recorded with this version." in page
    assert "## Limitations" in page
