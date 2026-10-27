"""Each rule fires just inside its threshold and not just outside it."""

from datetime import timedelta

from scoring_helpers import DAY, HOUR, MINUTE, NOW, RULES_CONFIG, SUBJECT, Market, routine, tx

from txrisk.features import SubjectWalletFeatures, Transaction, TransactionFeatures
from txrisk.scoring import load_rules_config
from txrisk.scoring.rules import rule_hits


def fires(code: str, market: Market, transaction: Transaction) -> bool:
    return code in market.codes(transaction)


# SIM_SWAP_DRAIN: a Cash-out or Transfer of at least half the balance within 24h of a SIM swap.


def test_sim_swap_drain_fires_inside_24_hours_for_half_the_balance():
    market = Market().record(*routine()).sim_swap(NOW - DAY + MINUTE)

    assert fires("SIM_SWAP_DRAIN", market, tx(transaction_type="cash_out", amount=500.0))


def test_sim_swap_drain_needs_the_sim_swap_within_24_hours():
    market = Market().record(*routine()).sim_swap(NOW - DAY - MINUTE)

    assert not fires("SIM_SWAP_DRAIN", market, tx(transaction_type="cash_out", amount=500.0))


def test_sim_swap_drain_needs_at_least_half_the_balance():
    market = Market().record(*routine()).sim_swap(NOW - HOUR)

    assert not fires("SIM_SWAP_DRAIN", market, tx(amount=499.0))


def test_sim_swap_drain_ignores_payments():
    market = Market().record(*routine()).sim_swap(NOW - HOUR)

    assert not fires("SIM_SWAP_DRAIN", market, tx(transaction_type="payment", amount=900.0))


def test_sim_swap_drain_fires_even_for_a_wallet_with_no_history():
    market = Market().sim_swap(NOW - HOUR)

    assert fires("SIM_SWAP_DRAIN", market, tx(amount=900.0))


# NEW_DEVICE_BURST: on a device first used within the hour, a third Transfer to a new
# Counterparty in that hour.


def _burst(first_device_use_minutes_ago: int, earlier_transfers: int) -> Market:
    start = NOW - first_device_use_minutes_ago * MINUTE
    earlier = [
        tx(start + i * MINUTE, counterparty=f"w-new-{i}", device="D-fraud")
        for i in range(earlier_transfers)
    ]
    return Market().record(*routine(), *earlier)


def test_new_device_burst_fires_on_the_third_new_counterparty_transfer_in_the_hour():
    burst = tx(counterparty="w-new-x", device="D-fraud")

    assert fires("NEW_DEVICE_BURST", _burst(30, 2), burst)


def test_new_device_burst_needs_three_transfers():
    burst = tx(counterparty="w-new-x", device="D-fraud")

    assert not fires("NEW_DEVICE_BURST", _burst(30, 1), burst)


def test_new_device_burst_needs_the_device_to_be_new_within_the_hour():
    burst = tx(counterparty="w-new-x", device="D-fraud")

    assert not fires("NEW_DEVICE_BURST", _burst(61, 2), burst)


def test_new_device_burst_waits_for_history():
    earlier = [
        tx(NOW - (10 - i) * MINUTE, counterparty=f"w-{i}", device="D-fraud") for i in range(2)
    ]
    burst = tx(counterparty="w-x", device="D-fraud")

    assert not fires("NEW_DEVICE_BURST", Market().record(*earlier), burst)


# MULE_CASHOUT: a Cash-out by a Wallet that received from at least 10 senders in the last 24h.


def _fan_in(senders: int) -> Market:
    incoming = [
        tx(NOW - 23 * HOUR + i * MINUTE, wallet=f"w-sender-{i}", counterparty=SUBJECT)
        for i in range(senders)
    ]
    return Market().record(*incoming)


def test_mule_cash_out_fires_after_ten_distinct_senders():
    assert fires("MULE_CASHOUT", _fan_in(10), tx(transaction_type="cash_out", counterparty="A1"))


def test_mule_cash_out_needs_ten_senders():
    assert not fires("MULE_CASHOUT", _fan_in(9), tx(transaction_type="cash_out", counterparty="A1"))


def test_mule_cash_out_is_only_about_cash_outs():
    assert not fires("MULE_CASHOUT", _fan_in(12), tx(counterparty="w-elsewhere"))


# AGENT_STRUCTURING: a near-limit Cash-out (within 10% below the limit) that makes at least five
# at the same Agent in 24h.


def _agent_with(near_limit_cash_outs: int) -> Market:
    earlier = [
        tx(NOW - (i + 1) * HOUR, "cash_out", amount=950.0, wallet=f"w-{i}", counterparty="A1")
        for i in range(near_limit_cash_outs)
    ]
    return Market().record(*earlier)


def test_agent_structuring_fires_on_the_fifth_near_limit_cash_out():
    cash_out = tx(transaction_type="cash_out", amount=900.0, counterparty="A1")

    assert fires("AGENT_STRUCTURING", _agent_with(4), cash_out)


def test_agent_structuring_needs_this_cash_out_near_the_limit():
    cash_out = tx(transaction_type="cash_out", amount=899.0, counterparty="A1")

    assert not fires("AGENT_STRUCTURING", _agent_with(4), cash_out)


def test_agent_structuring_needs_five_in_24_hours():
    cash_out = tx(transaction_type="cash_out", amount=950.0, counterparty="A1")

    assert not fires("AGENT_STRUCTURING", _agent_with(3), cash_out)


# FIRST_TIME_LARGE_TRANSFER: a Transfer to a new Counterparty of more than 3x the median.


def test_first_time_large_transfer_fires_above_three_times_the_median():
    market = Market().record(*routine())

    assert fires("FIRST_TIME_LARGE_TRANSFER", market, tx(amount=150.5, counterparty="w-new"))


def test_first_time_large_transfer_needs_more_than_three_times():
    market = Market().record(*routine())

    assert not fires("FIRST_TIME_LARGE_TRANSFER", market, tx(amount=150.0, counterparty="w-new"))


def test_first_time_large_transfer_needs_a_new_counterparty():
    market = Market().record(*routine())

    assert not fires("FIRST_TIME_LARGE_TRANSFER", market, tx(amount=200.0))


def _new_wallet(age: timedelta) -> Market:
    """The usual routine, in a Wallet registered `age` before now."""
    return Market(registered={SUBJECT: NOW - age}).record(*routine())


def test_first_time_large_transfer_waits_until_the_wallet_is_30_days_old():
    large = tx(amount=200.0, counterparty="w-new")

    assert not fires("FIRST_TIME_LARGE_TRANSFER", _new_wallet(30 * DAY - MINUTE), large)
    assert fires("FIRST_TIME_LARGE_TRANSFER", _new_wallet(30 * DAY), large)


def test_the_usual_amount_rules_still_fire_when_the_wallets_age_is_unknown():
    # A dataset without registration dates (PaySim) leaves the age empty.
    subject = SubjectWalletFeatures(
        history_count=6,
        wallet_age_days=None,  # type: ignore[arg-type]
        amount_to_median_30d=8.0,
        new_counterparty=True,
    )
    features = TransactionFeatures(subject_wallet=subject)
    rules = load_rules_config(RULES_CONFIG).rules

    codes = [h.code for h in rule_hits(tx(amount=400.0, counterparty="w-new"), features, rules)]

    assert {"FIRST_TIME_LARGE_TRANSFER", "AMOUNT_VS_MEDIAN"} <= set(codes)


# AMOUNT_VS_MEDIAN: an outgoing amount of more than 5x the 30-day median outgoing amount.


def test_amount_vs_median_fires_above_five_times_the_median():
    market = Market().record(*routine())

    assert fires("AMOUNT_VS_MEDIAN", market, tx(transaction_type="payment", amount=250.5))


def test_amount_vs_median_needs_more_than_five_times():
    market = Market().record(*routine())

    assert not fires("AMOUNT_VS_MEDIAN", market, tx(transaction_type="payment", amount=250.0))


def test_amount_vs_median_ignores_money_coming_in():
    market = Market().record(*routine())

    assert not fires("AMOUNT_VS_MEDIAN", market, tx(transaction_type="cash_in", amount=900.0))


def test_amount_vs_median_waits_until_the_wallet_is_30_days_old():
    large = tx(transaction_type="payment", amount=300.0)

    assert not fires("AMOUNT_VS_MEDIAN", _new_wallet(30 * DAY - MINUTE), large)
    assert fires("AMOUNT_VS_MEDIAN", _new_wallet(30 * DAY), large)


# NEAR_KYC_LIMIT: within 5% below the KYC tier's single-transaction limit.


def test_near_kyc_limit_fires_within_five_percent_of_the_limit():
    assert fires("NEAR_KYC_LIMIT", Market(), tx(transaction_type="payment", amount=950.0))


def test_near_kyc_limit_does_not_fire_further_below():
    assert not fires("NEAR_KYC_LIMIT", Market(), tx(transaction_type="payment", amount=949.0))


def test_near_kyc_limit_uses_the_wallets_own_tier():
    market = Market(tiers={SUBJECT: "enhanced"})

    assert not fires("NEAR_KYC_LIMIT", market, tx(transaction_type="payment", amount=990.0))


# NEW_REGION: a region the Wallet has not transacted in during the last 30 days.


def test_new_region_fires_away_from_the_usual_regions():
    market = Market().record(*routine())

    assert fires("NEW_REGION", market, tx(transaction_type="payment", region="Volta"))


def test_new_region_does_not_fire_at_home():
    market = Market().record(*routine())

    assert not fires("NEW_REGION", market, tx(transaction_type="payment"))


def test_new_region_waits_for_history():
    assert not fires("NEW_REGION", Market(), tx(transaction_type="payment", region="Volta"))


# ODD_HOUR: between 00:00 and 05:00, when under 5% of the last 30 days happened then.


def test_odd_hour_fires_before_five_for_a_daytime_wallet():
    market = Market().record(*routine())

    assert fires("ODD_HOUR", market, tx(NOW.replace(hour=4, minute=59)))


def test_odd_hour_does_not_fire_from_five():
    market = Market().record(*routine())

    assert not fires("ODD_HOUR", market, tx(NOW.replace(hour=5, minute=0)))


def test_odd_hour_does_not_fire_for_a_wallet_that_is_often_up_at_night():
    night_owl = Market().record(*routine(), tx((NOW - DAY / 2).replace(hour=2)))

    assert not fires("ODD_HOUR", night_owl, tx(NOW.replace(hour=3)))
