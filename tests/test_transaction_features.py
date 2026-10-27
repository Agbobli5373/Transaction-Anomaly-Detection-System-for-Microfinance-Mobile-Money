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
DAY = timedelta(days=1)
SECOND = timedelta(seconds=1)

SUBJECT = "w-subject"
MULE = "w-mule"
AGENT = "A00001"
# Single-transaction limits per KYC tier, as the market config sets them.
CONFIG = FeatureConfig(kyc_limits={"minimum": 1000.0, "medium": 5000.0})

_ids = iter(range(1_000_000))


def tx(
    at: datetime,
    transaction_type: str = "transfer",
    *,
    wallet: str = SUBJECT,
    counterparty: str = MULE,
    amount: float = 50.0,
    balance: float = 1000.0,
) -> Transaction:
    return Transaction(
        transaction_id=f"T{next(_ids):09d}",
        occurred_at=at,
        transaction_type=transaction_type,
        amount=amount,
        subject_wallet_id=wallet,
        direction="in" if transaction_type == "cash_in" else "out",
        counterparty_id=counterparty,
        region="Ashanti",
        subject_balance_before=balance,
    )


def transfer(sender: str, receiver: str, at: datetime) -> Transaction:
    return tx(at, wallet=sender, counterparty=receiver)


class Market:
    """Wallets known to the history, with their KYC tiers."""

    def __init__(self, **tiers: str) -> None:
        self.tiers = tiers

    def history(self, *transactions: Transaction) -> ReplayHistory:
        wallets = {
            t.subject_wallet_id: Wallet(registered_at=REGISTERED, kyc_tier="minimum")
            for t in transactions
        }
        wallets[SUBJECT] = Wallet(registered_at=REGISTERED, kyc_tier="minimum")
        for wallet, tier in self.tiers.items():
            wallets[wallet] = Wallet(registered_at=REGISTERED, kyc_tier=tier)
        history = ReplayHistory(wallets)
        for t in transactions:
            history.record(t)
        return history


def history_of(*transactions: Transaction) -> ReplayHistory:
    return Market().history(*transactions)


def test_a_transfer_sees_how_many_distinct_wallets_sent_to_its_receiver_in_the_last_24h():
    history = history_of(
        transfer("w-a", MULE, NOW - DAY - SECOND),
        transfer("w-b", MULE, NOW - DAY),
        transfer("w-c", MULE, NOW - timedelta(hours=3)),
        transfer("w-c", MULE, NOW - timedelta(hours=2)),
        transfer("w-d", MULE, NOW - SECOND),
        transfer("w-e", MULE, NOW),
        transfer("w-f", "w-elsewhere", NOW - timedelta(hours=1)),
        tx(NOW - timedelta(hours=1), "transfer", wallet=MULE, counterparty="w-g"),
    )

    features = transaction_features(transfer(SUBJECT, MULE, NOW), history, now=NOW, config=CONFIG)

    # w-b at the window's start, w-c twice and w-d; not w-a (too early) or w-e (same moment).
    assert features.receiver_fan_in_24h == 3


def test_a_cash_out_sees_how_many_distinct_wallets_sent_to_the_subject_wallet_in_the_last_24h():
    history = history_of(
        transfer("w-a", SUBJECT, NOW - DAY - SECOND),
        transfer("w-b", SUBJECT, NOW - timedelta(hours=20)),
        transfer("w-c", SUBJECT, NOW - timedelta(hours=10)),
        transfer("w-b", SUBJECT, NOW - timedelta(hours=1)),
        tx(NOW - timedelta(hours=5), "cash_in", counterparty=AGENT),
        transfer(SUBJECT, "w-z", NOW - timedelta(hours=4)),
        transfer("w-d", AGENT, NOW - timedelta(hours=3)),
    )

    features = transaction_features(
        tx(NOW, "cash_out", counterparty=AGENT), history, now=NOW, config=CONFIG
    )

    assert features.receiver_fan_in_24h == 2


def test_fan_in_is_empty_for_cash_ins_payments_and_bill_payments():
    history = history_of(transfer("w-a", SUBJECT, NOW - timedelta(hours=1)))

    def fan_in(transaction_type: str, counterparty: str) -> int | None:
        scored = tx(NOW, transaction_type, counterparty=counterparty)
        return transaction_features(scored, history, now=NOW, config=CONFIG).receiver_fan_in_24h

    assert fan_in("cash_in", AGENT) is None
    assert fan_in("payment", "M00001") is None
    assert fan_in("bill_payment", "B001") is None


def cash_out(wallet: str, at: datetime, amount: float, agent: str = AGENT) -> Transaction:
    return tx(at, "cash_out", wallet=wallet, counterparty=agent, amount=amount)


def test_cash_outs_and_cash_ins_see_the_agents_cash_outs_of_the_last_24h():
    history = history_of(
        cash_out("w-a", NOW - DAY - SECOND, 1.0),
        cash_out("w-a", NOW - DAY, 10.0),
        cash_out("w-b", NOW - timedelta(hours=6), 20.0),
        tx(NOW - timedelta(hours=5), "cash_in", wallet="w-c", counterparty=AGENT, amount=99.0),
        cash_out("w-c", NOW - timedelta(hours=4), 40.0, agent="A00002"),
        cash_out("w-a", NOW - SECOND, 70.0),
        cash_out("w-d", NOW, 500.0),
    )

    for transaction_type in ("cash_out", "cash_in"):
        scored = tx(NOW, transaction_type, counterparty=AGENT)
        features = transaction_features(scored, history, now=NOW, config=CONFIG)

        assert features.agent_cash_out_count_24h == 3
        assert features.agent_cash_out_amount_24h == 100.0


def test_agent_features_are_empty_for_transactions_without_an_agent():
    history = history_of(cash_out("w-a", NOW - timedelta(hours=1), 10.0))

    for transaction_type, counterparty in [
        ("transfer", MULE),
        ("payment", "M00001"),
        ("bill_payment", "B001"),
    ]:
        scored = tx(NOW, transaction_type, counterparty=counterparty)
        features = transaction_features(scored, history, now=NOW, config=CONFIG)

        assert features.agent_cash_out_count_24h is None
        assert features.agent_cash_out_amount_24h is None
        assert features.agent_near_limit_cash_out_count_24h is None


def test_near_limit_cash_outs_are_within_10_percent_below_their_wallets_kyc_tier_limit():
    market = Market(**{"w-min": "minimum", "w-med": "medium"})
    history = market.history(
        cash_out("w-min", NOW - DAY - SECOND, 950.0),
        cash_out("w-min", NOW - DAY, 900.0),
        cash_out("w-min", NOW - timedelta(hours=9), 899.99),
        cash_out("w-min", NOW - timedelta(hours=8), 1000.0),
        cash_out("w-min", NOW - timedelta(hours=7), 1000.01),
        cash_out("w-med", NOW - timedelta(hours=6), 950.0),
        cash_out("w-med", NOW - timedelta(hours=5), 4500.0),
        cash_out("w-min", NOW - timedelta(hours=4), 950.0, agent="A00002"),
        tx(NOW - timedelta(hours=3), "cash_in", wallet="w-min", counterparty=AGENT, amount=950.0),
        cash_out("w-min", NOW, 950.0),
    )

    features = transaction_features(
        tx(NOW, "cash_out", counterparty=AGENT), history, now=NOW, config=CONFIG
    )

    # 900 and 1000 against the 1000 limit, 4500 against the 5000 limit.
    assert features.agent_near_limit_cash_out_count_24h == 3


def test_the_near_limit_share_is_configurable():
    market = Market(**{"w-min": "minimum", "w-med": "medium"})
    history = market.history(
        cash_out("w-min", NOW - timedelta(hours=3), 850.0),
        cash_out("w-min", NOW - timedelta(hours=2), 950.0),
        cash_out("w-med", NOW - timedelta(hours=1), 4200.0),
    )
    scored = tx(NOW, "cash_in", counterparty=AGENT)
    wider = CONFIG.model_copy(update={"near_limit_share": 0.2})

    default = transaction_features(scored, history, now=NOW, config=CONFIG)
    widened = transaction_features(scored, history, now=NOW, config=wider)

    assert default.agent_near_limit_cash_out_count_24h == 1
    assert widened.agent_near_limit_cash_out_count_24h == 3


def test_time_since_sim_swap_measures_from_the_subject_wallets_latest_earlier_sim_swap():
    history = history_of()
    history.record_sim_swap(SUBJECT, NOW - timedelta(days=40))
    history.record_sim_swap(SUBJECT, NOW - timedelta(hours=2))
    history.record_sim_swap(SUBJECT, NOW)
    history.record_sim_swap(MULE, NOW - timedelta(minutes=5))

    features = transaction_features(tx(NOW), history, now=NOW, config=CONFIG)

    assert features.seconds_since_sim_swap == 7200.0


def test_time_since_sim_swap_is_empty_for_a_wallet_never_sim_swapped_before():
    history = history_of()
    history.record_sim_swap(SUBJECT, NOW + SECOND)
    history.record_sim_swap(MULE, NOW - timedelta(hours=1))

    features = transaction_features(tx(NOW), history, now=NOW, config=CONFIG)

    assert features.seconds_since_sim_swap is None


def test_share_of_balance_moved_is_the_amount_over_the_balance_before_for_outgoing_transactions():
    history = history_of()

    def share(transaction_type: str, amount: float, balance: float) -> float | None:
        scored = tx(NOW, transaction_type, amount=amount, balance=balance)
        return transaction_features(scored, history, now=NOW, config=CONFIG).share_of_balance_moved

    assert share("cash_out", 60.0, 80.0) == 0.75
    assert share("transfer", 80.0, 80.0) == 1.0
    assert share("payment", 5.0, 200.0) == 0.025
    assert share("bill_payment", 10.0, 40.0) == 0.25
    assert share("cash_in", 60.0, 80.0) is None
    assert share("transfer", 60.0, 0.0) is None


def test_later_transactions_and_sim_swaps_never_change_a_transactions_features():
    market = Market(**{"w-min": "minimum"})
    earlier = [
        transfer("w-a", MULE, NOW - timedelta(hours=5)),
        transfer("w-b", SUBJECT, NOW - timedelta(hours=4)),
        cash_out("w-min", NOW - timedelta(hours=3), 950.0),
        cash_out("w-c", NOW - timedelta(hours=2), 30.0),
    ]
    later = [
        transfer("w-x", MULE, NOW + timedelta(microseconds=1)),
        transfer("w-y", SUBJECT, NOW + timedelta(minutes=1)),
        cash_out("w-min", NOW + timedelta(minutes=2), 990.0),
        cash_out("w-z", NOW + timedelta(hours=1), 70.0),
    ]
    earlier_history = market.history(*earlier)
    earlier_history.record_sim_swap(SUBJECT, NOW - timedelta(hours=6))
    full_history = market.history(*earlier, *later)
    full_history.record_sim_swap(SUBJECT, NOW - timedelta(hours=6))
    full_history.record_sim_swap(SUBJECT, NOW + timedelta(minutes=3))

    for scored in (
        tx(NOW, "transfer", counterparty=MULE, amount=400.0, balance=500.0),
        tx(NOW, "cash_out", counterparty=AGENT, amount=400.0, balance=500.0),
    ):
        before = transaction_features(scored, earlier_history, now=NOW, config=CONFIG)
        after = transaction_features(scored, full_history, now=NOW, config=CONFIG)

        assert before.receiver_fan_in_24h == 1
        assert before.seconds_since_sim_swap == 21_600.0
        assert before.share_of_balance_moved == 0.8
        assert after == before

    assert before.agent_cash_out_count_24h == 2
    assert before.agent_cash_out_amount_24h == 980.0
    assert before.agent_near_limit_cash_out_count_24h == 1
