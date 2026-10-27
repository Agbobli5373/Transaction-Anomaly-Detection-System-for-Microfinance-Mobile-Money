"""Features the rules read: night-time activity, device age, bursts to new Counterparties and
how close an amount is to the KYC tier limit."""

from datetime import UTC, datetime, timedelta

from txrisk.features import (
    FeatureConfig,
    ReplayHistory,
    Transaction,
    Wallet,
    transaction_features,
)

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
REGISTERED = datetime(2030, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SUBJECT = "w-subject"
PHONE = "D-own-phone"
CONFIG = FeatureConfig(kyc_limits={"minimum": 1000.0})

_ids = iter(range(1_000_000))


def tx(
    at: datetime,
    *,
    counterparty: str = "w-friend",
    amount: float = 50.0,
    device: str = PHONE,
    transaction_type: str = "transfer",
) -> Transaction:
    return Transaction(
        transaction_id=f"T{next(_ids):09d}",
        occurred_at=at,
        transaction_type=transaction_type,
        amount=amount,
        subject_wallet_id=SUBJECT,
        direction="in" if transaction_type == "cash_in" else "out",
        counterparty_id=counterparty,
        region="Ashanti",
        subject_balance_before=1000.0,
        device_id=device,
    )


def features(scored: Transaction, *past: Transaction):
    history = ReplayHistory({SUBJECT: Wallet(registered_at=REGISTERED, kyc_tier="minimum")})
    for t in past:
        history.record(t)
    return transaction_features(scored, history, now=scored.occurred_at, config=CONFIG)


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2030, 5, day, hour, minute, tzinfo=UTC)


def test_night_share_is_the_share_of_the_last_30_days_between_midnight_and_five():
    past = [
        tx(_at(10, 0)),  # night
        tx(_at(11, 4, 59)),  # night: before 05:00
        tx(_at(12, 5)),  # 05:00 is no longer night
        *[tx(_at(13 + d, 14)) for d in range(7)],
        tx(datetime(2030, 4, 1, 1, tzinfo=UTC)),  # night, but outside the 30-day Profile
    ]

    assert features(tx(NOW), *past).subject_wallet.night_share_30d == 2 / 10


def test_night_share_is_empty_in_cold_start():
    assert features(tx(NOW), tx(NOW - DAY)).subject_wallet.night_share_30d is None


def test_device_age_counts_from_the_devices_first_use_on_this_wallet():
    past = [
        tx(NOW - 3 * DAY, device="D-old"),
        tx(NOW - 40 * timedelta(minutes=1), device="D-fraud"),
        tx(NOW - 10 * timedelta(minutes=1), device="D-fraud"),
    ]

    assert features(tx(NOW, device="D-fraud"), *past).device_age_seconds == 40 * 60
    assert features(tx(NOW, device="D-never-seen"), *past).device_age_seconds == 0


def test_bursts_count_recent_transfers_to_counterparties_that_were_new_at_the_time():
    past = [
        tx(NOW - 2 * DAY, counterparty="w-friend"),
        tx(NOW - 2 * HOUR, counterparty="w-old-new"),  # new then, but over an hour ago
        tx(NOW - HOUR, counterparty="w-a"),  # new, exactly an hour ago: in the window
        tx(NOW - 30 * timedelta(minutes=1), counterparty="w-b"),  # new
        tx(NOW - 20 * timedelta(minutes=1), counterparty="w-a"),  # w-a again: not new now
        tx(NOW - 10 * timedelta(minutes=1), counterparty="w-friend"),  # known
        tx(NOW - 5 * timedelta(minutes=1), counterparty="A1", transaction_type="cash_out"),
    ]

    assert features(tx(NOW), *past).new_counterparty_transfers_1h == 2


def test_amount_is_measured_against_the_subject_wallets_kyc_tier_limit():
    assert features(tx(NOW, amount=950.0)).amount_to_kyc_limit == 0.95


def test_rule_features_ignore_anything_at_or_after_the_scoring_moment():
    scored = tx(NOW, device="D-fraud")
    past = [tx(NOW - DAY * d, device=PHONE) for d in range(1, 8)]
    later = [
        tx(NOW, counterparty="w-z", device="D-fraud"),
        tx(NOW + HOUR, counterparty="w-y", device="D-fraud"),
        tx(datetime(2030, 6, 2, 1, tzinfo=UTC)),
    ]

    assert features(scored, *past) == features(scored, *past, *later)
