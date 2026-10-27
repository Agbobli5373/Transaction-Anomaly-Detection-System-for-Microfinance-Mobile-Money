"""Reasons for a stacked Risk score: Rule hits and the inputs that pushed the models up."""

import math

import pandas as pd
from scoring_helpers import RULES_CONFIG

from txrisk.models import INPUT_COLUMNS
from txrisk.scoring import RULES, Reason, RuleHit, Severity, load_rules_config
from txrisk.scoring.reasons import INPUT_REASONS, UNEXPLAINED_INPUTS, stacked_reasons

CONFIG = load_rules_config(RULES_CONFIG)

DRAIN = RuleHit("SIM_SWAP_DRAIN", Severity.CRITICAL, "Moved 90% of the balance", 0.9)
AMOUNT = RuleHit("AMOUNT_VS_MEDIAN", Severity.MEDIUM, "Amount is 8.0x this Wallet's median", 8.0)
NIGHT = RuleHit("ODD_HOUR", Severity.LOW, "At 03:00", 0.01)


def _inputs(**values: float) -> pd.Series:
    """Model inputs of an ordinary Transaction, with `values` set; history features are empty."""
    return pd.Series({c: math.nan for c in INPUT_COLUMNS} | values, dtype=float)


def _push(**contributions: float) -> pd.Series:
    """How much each input pushed the Risk score's log-odds; unnamed inputs did nothing."""
    return pd.Series({c: 0.0 for c in INPUT_COLUMNS} | contributions, dtype=float)


def _codes(reasons: tuple[Reason, ...]) -> list[str]:
    return [r.code for r in reasons]


def test_every_model_input_has_a_reason_or_is_known_not_to_need_one():
    assert set(INPUT_REASONS) | UNEXPLAINED_INPUTS == set(INPUT_COLUMNS)
    assert not set(INPUT_REASONS) & UNEXPLAINED_INPUTS


def test_inputs_that_measure_what_a_rule_measures_share_its_code():
    codes = {INPUT_REASONS[c].code for c in INPUT_REASONS}

    assert INPUT_REASONS["amount_to_median_30d"].code == "AMOUNT_VS_MEDIAN"
    assert INPUT_REASONS["amount_to_kyc_limit"].code == "NEAR_KYC_LIMIT"
    assert INPUT_REASONS["new_region_30d"].code == "NEW_REGION"
    assert all(code.isupper() and " " not in code for code in codes)
    assert {"AMOUNT_VS_MEDIAN", "NEAR_KYC_LIMIT", "NEW_REGION"} <= codes & set(RULES)


def test_the_inputs_that_pushed_hardest_are_the_reasons():
    reasons = stacked_reasons(
        [],
        rules_contribution=0.0,
        inputs=_inputs(receiver_fan_in_24h=14, wallet_age_days=3, share_of_balance_moved=0.4),
        contributions=_push(receiver_fan_in_24h=1.2, share_of_balance_moved=0.3, wallet_age_days=2),
        config=CONFIG,
    )

    assert _codes(reasons) == ["NEW_WALLET", "RECEIVER_FAN_IN", "SHARE_OF_BALANCE"]
    assert reasons[1] == Reason(
        "RECEIVER_FAN_IN",
        "The receiving Wallet got money from 14 different Wallets in 24 hours",
        14,
    )


def test_a_critical_rule_hit_comes_first_however_little_it_counts():
    reasons = stacked_reasons(
        [AMOUNT, DRAIN],
        rules_contribution=0.1,
        inputs=_inputs(wallet_age_days=2, receiver_fan_in_24h=20),
        contributions=_push(wallet_age_days=3.0, receiver_fan_in_24h=2.0),
        config=CONFIG,
    )

    assert _codes(reasons) == ["SIM_SWAP_DRAIN", "NEW_WALLET", "RECEIVER_FAN_IN"]


def test_rule_hits_share_the_rules_contribution_by_severity():
    # 1.15 split 0.25 : 0.10 gives AMOUNT_VS_MEDIAN about 0.82 and ODD_HOUR about 0.33.
    reasons = stacked_reasons(
        [NIGHT, AMOUNT],
        rules_contribution=1.15,
        inputs=_inputs(wallet_age_days=20),
        contributions=_push(wallet_age_days=0.5),
        config=CONFIG,
    )

    assert _codes(reasons) == ["AMOUNT_VS_MEDIAN", "NEW_WALLET", "ODD_HOUR"]


def test_a_model_reason_with_a_rule_s_code_is_one_reason_in_the_rule_s_words():
    reasons = stacked_reasons(
        [NIGHT, AMOUNT],
        rules_contribution=0.35,
        inputs=_inputs(amount_to_median_30d=8.0, wallet_age_days=20),
        # On its own AMOUNT_VS_MEDIAN (0.25 from the rule) would come after NEW_WALLET.
        contributions=_push(amount_to_median_30d=0.5, wallet_age_days=0.6),
        config=CONFIG,
    )

    assert _codes(reasons) == ["AMOUNT_VS_MEDIAN", "NEW_WALLET", "ODD_HOUR"]
    assert reasons[0] == Reason(AMOUNT.code, AMOUNT.message, AMOUNT.value)


def test_inputs_that_lowered_risk_were_empty_or_were_false_are_not_reasons():
    reasons = stacked_reasons(
        [],
        rules_contribution=0.0,
        inputs=_inputs(wallet_age_days=400, new_counterparty=0, log_amount=math.log1p(50)),
        contributions=_push(
            wallet_age_days=-1.0,  # an old Wallet makes fraud less likely
            new_counterparty=0.8,  # a known Counterparty is no reason for alarm
            amount_to_median_30d=0.9,  # no history to compare with
            log_amount=0.2,
        ),
        config=CONFIG,
    )

    assert reasons == (Reason("LARGE_AMOUNT", "Amount of 50 GHS", 50.0),)


def test_reasons_are_capped():
    reasons = stacked_reasons(
        [NIGHT],
        rules_contribution=0.5,
        inputs=_inputs(wallet_age_days=2, receiver_fan_in_24h=20, share_of_balance_moved=0.9),
        contributions=_push(wallet_age_days=1, receiver_fan_in_24h=1, share_of_balance_moved=1),
        config=CONFIG,
    )

    assert len(reasons) == CONFIG.max_reasons
