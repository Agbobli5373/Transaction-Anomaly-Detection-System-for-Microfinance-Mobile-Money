from datetime import UTC, datetime, timedelta

from txrisk.features import (
    ReplayHistory,
    SubjectWalletFeatures,
    Transaction,
    Wallet,
    subject_wallet_features,
)

WALLET = "w-subject"
REGISTERED = datetime(2030, 3, 1, tzinfo=UTC)
NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)

_ids = iter(range(1_000_000))


def tx(
    at: datetime,
    amount: float = 50.0,
    *,
    transaction_type: str = "transfer",
    counterparty: str = "w-friend",
    region: str = "Ashanti",
    wallet: str = WALLET,
) -> Transaction:
    return Transaction(
        transaction_id=f"T{next(_ids):09d}",
        occurred_at=at,
        transaction_type=transaction_type,
        amount=amount,
        subject_wallet_id=wallet,
        direction="in" if transaction_type == "cash_in" else "out",
        counterparty_id=counterparty,
        region=region,
        subject_balance_before=1000.0,
    )


def history_of(*transactions: Transaction) -> ReplayHistory:
    history = ReplayHistory({WALLET: Wallet(registered_at=REGISTERED, kyc_tier="minimum")})
    for t in transactions:
        history.record(t)
    return history


def test_a_wallet_without_history_has_only_history_count_and_wallet_age():
    features = subject_wallet_features(tx(NOW), history_of(), now=NOW)

    assert features == SubjectWalletFeatures(history_count=0, wallet_age_days=92.5)


def test_velocity_windows_include_their_start_and_exclude_the_scoring_moment():
    hour, day, week, second = (
        timedelta(hours=1),
        timedelta(days=1),
        timedelta(days=7),
        timedelta(seconds=1),
    )
    history = history_of(
        tx(NOW - hour, 10.0),
        tx(NOW - hour - second, 20.0),
        tx(NOW - day, 40.0),
        tx(NOW - day - second, 80.0),
        tx(NOW - week, 160.0),
        tx(NOW - week - second, 320.0),
        tx(NOW, 640.0),
    )

    features = subject_wallet_features(tx(NOW), history, now=NOW)

    assert features.history_count == 6
    assert (features.velocity_count_1h, features.velocity_amount_1h) == (1, 10.0)
    assert (features.velocity_count_24h, features.velocity_amount_24h) == (3, 70.0)
    assert (features.velocity_count_7d, features.velocity_amount_7d) == (5, 310.0)


def _days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def test_history_dependent_features_stay_empty_below_the_minimum_history():
    four = history_of(*(tx(_days_ago(d)) for d in (1, 2, 3, 4)))
    five = history_of(*(tx(_days_ago(d)) for d in (1, 2, 3, 4, 5)))

    cold = subject_wallet_features(tx(NOW), four, now=NOW)
    warm = subject_wallet_features(tx(NOW), five, now=NOW)

    assert cold == SubjectWalletFeatures(history_count=4, wallet_age_days=92.5)
    assert warm.velocity_count_7d == 5


def test_the_minimum_history_is_configurable():
    two = history_of(tx(_days_ago(1)), tx(_days_ago(2)))

    features = subject_wallet_features(tx(NOW), two, now=NOW, min_history=2)

    assert features.velocity_count_7d == 2


def test_amount_is_compared_with_the_median_outgoing_amount_of_the_last_30_days():
    history = history_of(
        tx(_days_ago(30) - timedelta(seconds=1), 5000.0),
        tx(_days_ago(30), 50.0),
        tx(_days_ago(20), 10.0),
        tx(_days_ago(10), 1000.0, transaction_type="cash_in", counterparty="A00001"),
        tx(_days_ago(5), 30.0, transaction_type="payment", counterparty="M00001"),
        tx(_days_ago(3), 20.0, transaction_type="cash_out", counterparty="A00001"),
        tx(_days_ago(1), 40.0, transaction_type="bill_payment", counterparty="B001"),
    )

    features = subject_wallet_features(tx(NOW, 90.0), history, now=NOW)

    assert features.amount_to_median_30d == 3.0


def test_amount_is_compared_with_the_largest_cash_in_of_the_last_30_days():
    def cash_in(at: datetime, amount: float) -> Transaction:
        return tx(at, amount, transaction_type="cash_in", counterparty="A00001")

    history = history_of(
        cash_in(_days_ago(30) - timedelta(seconds=1), 5000.0),
        cash_in(_days_ago(30), 200.0),
        cash_in(_days_ago(10), 100.0),
        tx(_days_ago(5), 900.0),
        tx(_days_ago(3), 600.0, transaction_type="cash_out", counterparty="A00001"),
    )

    features = subject_wallet_features(cash_in(NOW, 950.0), history, now=NOW)

    # The 5000 GHS Cash-in is just outside the window; outgoing amounts never count.
    assert features.amount_to_largest_cash_in_30d == 4.75


def test_largest_cash_in_ratio_is_empty_when_the_wallet_had_no_cash_in_in_30_days():
    history = history_of(
        tx(_days_ago(31), 100.0, transaction_type="cash_in", counterparty="A00001"),
        *(tx(_days_ago(d), 100.0) for d in (4, 3, 2, 1)),
    )

    features = subject_wallet_features(tx(NOW), history, now=NOW)

    assert features.history_count == 5
    assert features.amount_to_largest_cash_in_30d is None


def test_time_since_the_wallets_last_transaction():
    history = history_of(*(tx(_days_ago(d)) for d in (5, 4, 3, 2)), tx(NOW - timedelta(hours=3)))

    features = subject_wallet_features(tx(NOW), history, now=NOW)

    assert features.seconds_since_last_transaction == 10_800.0


def test_a_counterparty_is_new_only_if_the_wallet_never_transacted_with_it_before():
    history = history_of(
        tx(_days_ago(90), counterparty="w-old-friend"),
        *(tx(_days_ago(d), counterparty="A00001") for d in (4, 3, 2, 1)),
        tx(NOW, counterparty="w-same-moment"),
    )

    def new_counterparty(counterparty: str) -> bool | None:
        scored = tx(NOW, counterparty=counterparty)
        return subject_wallet_features(scored, history, now=NOW).new_counterparty

    assert new_counterparty("w-old-friend") is False
    assert new_counterparty("A00001") is False
    assert new_counterparty("w-stranger") is True
    assert new_counterparty("w-same-moment") is True


def test_an_hour_is_unusual_when_the_wallet_has_not_transacted_within_an_hour_of_it_lately():
    history = history_of(
        tx(datetime(2030, 4, 25, 3, 0, tzinfo=UTC)),
        tx(datetime(2030, 5, 20, 9, 10, tzinfo=UTC)),
        tx(datetime(2030, 5, 21, 10, 45, tzinfo=UTC)),
        tx(datetime(2030, 5, 22, 23, 30, tzinfo=UTC)),
        tx(datetime(2030, 5, 25, 9, 0, tzinfo=UTC)),
    )

    def unusual_hour(hour: int, minute: int) -> bool | None:
        at = datetime(2030, 6, 1, hour, minute, tzinfo=UTC)
        return subject_wallet_features(tx(at), history, now=at).unusual_hour

    assert unusual_hour(8, 0) is False
    assert unusual_hour(11, 59) is False
    assert unusual_hour(0, 15) is False
    assert unusual_hour(12, 0) is True
    assert unusual_hour(3, 0) is True


def test_a_region_is_new_when_the_wallet_has_not_transacted_there_in_30_days():
    history = history_of(
        tx(_days_ago(30) - timedelta(seconds=1), region="Volta"),
        tx(_days_ago(30), region="Oti"),
        *(tx(_days_ago(d), region="Ashanti") for d in (4, 3, 2, 1)),
    )

    def new_region(region: str) -> bool | None:
        return subject_wallet_features(tx(NOW, region=region), history, now=NOW).new_region_30d

    assert new_region("Ashanti") is False
    assert new_region("Oti") is False
    assert new_region("Volta") is True
    assert new_region("Northern") is True


def test_transactions_later_than_the_one_being_scored_never_change_its_features():
    earlier = [
        tx(_days_ago(40), 70.0, counterparty="w-old-friend", region="Volta"),
        tx(_days_ago(6), 25.0, transaction_type="cash_in", counterparty="A00001"),
        tx(_days_ago(2), 15.0, transaction_type="payment", counterparty="M00001"),
        tx(NOW - timedelta(hours=5), 35.0),
        tx(NOW - timedelta(minutes=30), 45.0, transaction_type="cash_out", counterparty="A00001"),
        tx(NOW - timedelta(minutes=1), 55.0, counterparty="w-other", region="Oti"),
    ]
    later = [
        tx(NOW + timedelta(microseconds=1), 9000.0, counterparty="w-stranger", region="Bono"),
        tx(NOW + timedelta(minutes=10), 1.0, counterparty="w-stranger", region="Bono"),
        tx(NOW + timedelta(days=3), 500.0, transaction_type="cash_in", region="Savannah"),
    ]
    scored = tx(NOW, 80.0, counterparty="w-stranger", region="Bono")

    before = subject_wallet_features(scored, history_of(*earlier), now=NOW)
    after = subject_wallet_features(scored, history_of(*earlier, *later), now=NOW)

    assert before.velocity_count_7d == 5
    assert before.amount_to_largest_cash_in_30d == 3.2
    assert after == before


def test_features_are_measured_from_the_clock_given_not_the_wall_clock():
    history = history_of(*(tx(_days_ago(d)) for d in (5, 4, 3, 2, 1)))
    a_year_later = NOW + timedelta(days=365)

    now_features = subject_wallet_features(tx(NOW), history, now=NOW)
    later_features = subject_wallet_features(tx(a_year_later), history, now=a_year_later)

    assert now_features.wallet_age_days == 92.5
    assert later_features.wallet_age_days == 457.5
    assert later_features.seconds_since_last_transaction == 366 * 86_400.0


def test_worked_example_of_every_subject_wallet_feature():
    def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2030, month, day, hour, minute, tzinfo=UTC)

    history = history_of(
        tx(at(4, 20, 8), 200.0, counterparty="w-friend"),
        tx(at(5, 20, 7, 30), 300.0, transaction_type="cash_in", counterparty="A00001"),
        tx(at(5, 28, 18), 40.0, transaction_type="payment", counterparty="M00001"),
        tx(
            at(5, 31, 13),
            20.0,
            transaction_type="bill_payment",
            counterparty="B001",
            region="Greater Accra",
        ),
        tx(at(6, 1, 11, 15), 60.0, counterparty="w-friend"),
    )
    scored = tx(NOW, 120.0, counterparty="w-new", region="Eastern")

    features = subject_wallet_features(scored, history, now=NOW)

    assert features == SubjectWalletFeatures(
        history_count=5,
        wallet_age_days=92.5,
        velocity_count_1h=1,
        velocity_amount_1h=60.0,
        velocity_count_24h=2,
        velocity_amount_24h=80.0,
        velocity_count_7d=3,
        velocity_amount_7d=120.0,
        amount_to_median_30d=3.0,
        amount_to_largest_cash_in_30d=0.4,
        seconds_since_last_transaction=2700.0,
        new_counterparty=True,
        unusual_hour=False,
        new_region_30d=True,
        # The four Profile Transactions (07:30, 18:00, 13:00, 11:15) are all daytime.
        night_share_30d=0.0,
    )


def test_amount_ratio_is_empty_when_the_wallet_sent_nothing_in_30_days():
    history = history_of(
        tx(_days_ago(45), 100.0),
        *(tx(_days_ago(d), 100.0, transaction_type="cash_in") for d in (4, 3, 2, 1)),
    )

    features = subject_wallet_features(tx(NOW), history, now=NOW)

    assert features.history_count == 5
    assert features.amount_to_median_30d is None
