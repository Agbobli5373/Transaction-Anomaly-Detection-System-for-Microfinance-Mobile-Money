"""What a Risk assessment says: score, band, Reasons and components."""

import numpy as np
import pandas as pd
from scoring_helpers import DAY, FEATURES, HOUR, MINUTE, NOW, Market, routine, tx

from txrisk.features import transaction_features
from txrisk.models import INPUT_COLUMNS, inputs_for
from txrisk.models.anomaly import AnomalyModel
from txrisk.scoring import Reason, RiskAssessment, RiskBand


def test_an_ordinary_transaction_is_low_risk_with_no_reasons():
    assessment = Market().record(*routine()).assess(tx())

    assert assessment == RiskAssessment(
        transaction_id=assessment.transaction_id,
        risk_score=0,
        risk_band=RiskBand.LOW,
        reasons=(),
        component_scores={"rules": 0.0, "anomaly": None, "supervised": None},
        model_version="rules-only",
        degraded=False,
    )


def test_one_medium_rule_makes_a_medium_risk():
    assessment = Market().record(*routine()).assess(tx(transaction_type="payment", amount=300.0))

    assert [r.code for r in assessment.reasons] == ["AMOUNT_VS_MEDIAN"]
    assert assessment.risk_score == 25
    assert assessment.risk_band == RiskBand.MEDIUM


def test_two_low_rules_stay_low_and_three_reach_medium():
    # A Wallet that usually pays about 900 GHS, so large amounts are normal for it.
    big_spender = [tx(NOW - DAY * d, "payment", amount=900.0) for d in range(6, 0, -1)]
    market = Market().record(*big_spender)
    night_in_volta = NOW.replace(hour=3)

    # NEW_REGION and ODD_HOUR, both low: 1 - 0.9 * 0.9 = 0.19.
    two = market.assess(tx(night_in_volta, "payment", amount=900.0, region="Volta"))
    # The same, but within 5% of the 1,000 GHS limit too: 1 - 0.9**3 = 0.271.
    three = market.assess(tx(night_in_volta, "payment", amount=960.0, region="Volta"))

    assert [r.code for r in two.reasons] == ["NEW_REGION", "ODD_HOUR"]
    assert (two.risk_score, two.risk_band) == (19, RiskBand.LOW)
    assert [r.code for r in three.reasons] == ["NEAR_KYC_LIMIT", "NEW_REGION", "ODD_HOUR"]
    assert (three.risk_score, three.risk_band) == (27, RiskBand.MEDIUM)


def test_one_high_rule_makes_a_high_risk():
    incoming = [
        tx(NOW - 2 * HOUR + i * MINUTE, wallet=f"w-sender-{i}", counterparty="w-subject")
        for i in range(12)
    ]
    assessment = Market().record(*incoming).assess(tx(transaction_type="cash_out"))

    assert [r.code for r in assessment.reasons] == ["MULE_CASHOUT"]
    assert assessment.risk_score == 50
    assert assessment.risk_band == RiskBand.HIGH


def test_a_sim_swap_drain_is_always_high_and_its_reason_comes_first():
    market = Market().record(*routine())
    # Also a first-time Transfer of 18x the median at 03:00 in a new region.
    drain = tx(NOW.replace(hour=3), amount=900.0, counterparty="w-stranger", region="Volta")
    market.sim_swap(drain.occurred_at - 2 * HOUR)

    assessment = market.assess(drain)

    assert assessment.risk_band == RiskBand.HIGH
    assert assessment.risk_score >= 90
    assert assessment.reasons[0].code == "SIM_SWAP_DRAIN"


def test_reasons_are_capped_at_three_heaviest_first():
    market = Market().record(*routine())
    drain = tx(NOW.replace(hour=3), amount=900.0, counterparty="w-stranger", region="Volta")
    market.sim_swap(drain.occurred_at - 2 * HOUR)

    reasons = market.assess(drain).reasons

    # Fires: SIM_SWAP_DRAIN (critical), FIRST_TIME_LARGE_TRANSFER and AMOUNT_VS_MEDIAN
    # (medium), NEW_REGION and ODD_HOUR (low). Only the first three are returned.
    assert [r.code for r in reasons] == [
        "SIM_SWAP_DRAIN",
        "FIRST_TIME_LARGE_TRANSFER",
        "AMOUNT_VS_MEDIAN",
    ]


def test_each_reason_states_the_value_behind_it():
    market = Market().record(*routine()).sim_swap(NOW - 2 * HOUR)

    reasons = market.assess(tx(transaction_type="cash_out", amount=820.0)).reasons

    assert reasons[0] == Reason(
        code="SIM_SWAP_DRAIN",
        message="Moved 82% of the balance 2.0 hours after a SIM swap",
        value=0.82,
    )
    assert reasons[1] == Reason(
        code="AMOUNT_VS_MEDIAN", message="Amount is 16.4x this Wallet's 30-day median", value=16.4
    )


def test_a_new_wallet_is_not_judged_on_history_it_does_not_have():
    # Five history-dependent rules would fire for this Transfer if the Wallet had a routine.
    assessment = Market().assess(
        tx(NOW.replace(hour=3), amount=600.0, counterparty="w-stranger", region="Volta")
    )

    assert assessment.reasons == ()
    assert assessment.risk_band == RiskBand.LOW


def test_nothing_after_the_transaction_changes_its_assessment():
    scored = tx(NOW.replace(hour=3), amount=600.0, counterparty="w-stranger", region="Volta")
    later = [
        tx(NOW, amount=600.0, counterparty="w-stranger", region="Volta"),
        tx(NOW + HOUR, transaction_type="cash_in", amount=900.0),
        tx(NOW + DAY, region="Volta"),
    ]
    before = Market().record(*routine())
    after = Market().record(*routine(), *later)
    after.sim_swap(NOW + MINUTE)

    assert before.assess(scored) == after.assess(scored)


def test_the_anomaly_score_is_reported_alongside_the_rules():
    rng = np.random.default_rng(0)
    training = pd.DataFrame(rng.normal(1, 0.1, (500, len(INPUT_COLUMNS))), columns=INPUT_COLUMNS)
    model = AnomalyModel.fit(training, seed=1)
    market = Market().record(*routine())
    scored = tx(transaction_type="payment", amount=300.0)

    with_model = market.assess(scored, anomaly=model)
    rules_only = market.assess(scored)

    features = transaction_features(scored, market.history, now=scored.occurred_at, config=FEATURES)
    expected = model.score(inputs_for(scored, features)).iloc[0]
    assert with_model.component_scores["anomaly"] == expected
    assert rules_only.component_scores["anomaly"] is None
    # Without a model bundle, the rules alone set the Risk score and band.
    assert with_model.risk_score == rules_only.risk_score
    assert with_model.risk_band == rules_only.risk_band
