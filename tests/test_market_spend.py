"""The market spend index: how busy and large the whole market's spending is, from history alone."""

from datetime import UTC, datetime, timedelta

import pytest

from txrisk.features import (
    FeatureConfig,
    ReplayHistory,
    Transaction,
    Wallet,
    transaction_features,
)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
# 10:30 on the eleventh day: the market has a week and more of history behind it.
NOW = datetime(2030, 6, 11, 10, 30, tzinfo=UTC)
START = datetime(2030, 6, 1, tzinfo=UTC)
CONFIG = FeatureConfig(kyc_limits={"minimum": 1000.0})
SUBJECT = "w-subject"

_ids = iter(range(1_000_000))


def tx(at: datetime, amount: float) -> Transaction:
    return Transaction(
        transaction_id=f"T{next(_ids):09d}",
        occurred_at=at,
        transaction_type="payment",
        amount=amount,
        subject_wallet_id=SUBJECT,
        direction="out",
        counterparty_id="M00001",
        region="Ashanti",
        subject_balance_before=1000.0,
    )


def market(spend: dict[datetime, float]) -> ReplayHistory:
    """A market that spends the given GHS in each hour (one Transaction an hour), the rest of the
    hours from the start of the data until NOW at a steady 100."""
    history = ReplayHistory({SUBJECT: Wallet(registered_at=START, kyc_tier="minimum")})
    hour = START
    while hour < NOW + 3 * HOUR:
        history.record(tx(hour + timedelta(minutes=5), spend.get(hour, 100.0)))
        hour += HOUR
    return history


def index_at(history: ReplayHistory, now: datetime = NOW) -> float | None:
    return transaction_features(tx(now, 50.0), history, now=now, config=CONFIG).market_spend_index


def this_hour() -> datetime:
    return NOW.replace(minute=0)


def test_a_market_spending_as_it_always_does_has_an_index_of_one():
    assert index_at(market({})) == 1.0


def test_a_market_with_no_history_has_no_index():
    history = ReplayHistory({SUBJECT: Wallet(registered_at=START, kyc_tier="minimum")})

    assert index_at(history) is None


def test_a_busy_morning_shows_at_once_in_the_last_six_hours():
    busy = {this_hour() - HOUR * h: 200.0 for h in range(1, 7)}

    # The last 6 hours are twice a usual morning, though the last 24 hours are only 1.25 times.
    assert index_at(market(busy)) == 2.0


def test_a_busy_day_fades_slowly_after_it_ends():
    # Spending was 3 times usual from 22 to 12 hours ago, and is back to normal for the last 12.
    busy = {this_hour() - HOUR * h: 300.0 for h in range(12, 23)}

    # The last 6 hours are usual again, but the last 24 still remember the busy day.
    assert index_at(market(busy)) == pytest.approx(1 + 2 * 11 / 24)


def test_the_index_compares_with_the_same_hours_on_earlier_days():
    # The market always spends 10 times as much at 10:00 as at 4:00. A normal morning is not busy.
    spend = {
        START + DAY * d + HOUR * h: 1000.0 if 7 <= h <= 12 else 100.0
        for d in range(11)
        for h in range(24)
    }

    assert index_at(market(spend)) == 1.0


def test_only_complete_hours_before_the_transaction_are_counted():
    base = market({})
    usual = index_at(base)

    # A huge Transaction already made in the Transaction's own hour, and others in later hours.
    base.record(tx(NOW - timedelta(minutes=10), 1_000_000.0))
    base.record(tx(NOW + HOUR, 1_000_000.0))

    assert index_at(base) == usual


def test_a_baseline_day_before_the_data_begins_is_left_out_of_the_comparison():
    # Only three days of history, at a steady rate: the missing days must not look like quiet ones.
    history = ReplayHistory({SUBJECT: Wallet(registered_at=START, kyc_tier="minimum")})
    start = NOW - 3 * DAY
    hour = start.replace(minute=0)
    while hour < NOW:
        history.record(tx(hour + timedelta(minutes=5), 100.0))
        hour += HOUR

    assert index_at(history) == 1.0
