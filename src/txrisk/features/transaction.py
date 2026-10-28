"""Every feature of one Transaction: the one feature function training and serving share."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from txrisk.features.config import FeatureConfig
from txrisk.features.history import HistoryProvider, Transaction
from txrisk.features.subject_wallet import SubjectWalletFeatures, subject_wallet_features

_DAY = timedelta(days=1)
_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class TransactionFeatures:
    subject_wallet: SubjectWalletFeatures
    # Distinct Wallets that Transferred to the receiving Wallet in the last 24h; for a Cash-out
    # the receiving side is the Subject wallet itself. Empty for other Transaction types.
    receiver_fan_in_24h: int | None = None
    # For a Transfer, what its Counterparty Transferred to the Subject wallet in the last 24h:
    # money coming back the other way. Empty for other Transaction types.
    received_from_counterparty_24h: float | None = None
    # The Agent's Cash-outs in the last 24h, for Cash-ins and Cash-outs; empty otherwise.
    agent_cash_out_count_24h: int | None = None
    agent_cash_out_amount_24h: float | None = None
    agent_near_limit_cash_out_count_24h: int | None = None
    # Empty when the Subject wallet had no SIM swap before the Transaction.
    seconds_since_sim_swap: float | None = None
    # Amount over the Subject wallet's balance before, for outgoing Transactions; empty for
    # incoming ones and when the balance was zero.
    share_of_balance_moved: float | None = None
    # Seconds since this device was first used by the Subject wallet; 0 when it never was.
    device_age_seconds: float = 0.0
    # Transfers by the Subject wallet in the last hour to Counterparties new to it at the time.
    new_counterparty_transfers_1h: int = 0
    # Amount over the Subject wallet's KYC tier single-transaction limit.
    amount_to_kyc_limit: float = 0.0


def _fan_in(wallet_id: str, history: HistoryProvider, now: datetime) -> int:
    transfers = history.incoming_transfers(wallet_id, since=now - _DAY, before=now)
    return len({t.subject_wallet_id for t in transfers})


def _received_from(
    wallet_id: str, sender_id: str, history: HistoryProvider, now: datetime
) -> float:
    transfers = history.incoming_transfers(wallet_id, since=now - _DAY, before=now)
    return sum((t.amount for t in transfers if t.subject_wallet_id == sender_id), 0.0)


def _device_age(transaction: Transaction, past: Sequence[Transaction], now: datetime) -> float:
    first_use = next((t.occurred_at for t in past if t.device_id == transaction.device_id), None)
    return 0.0 if first_use is None else (now - first_use).total_seconds()


def _new_counterparty_transfers(past: Sequence[Transaction], now: datetime) -> int:
    """Transfers in the last hour whose Counterparty the Wallet had never dealt with before."""
    window_start = now - _HOUR
    known = {t.counterparty_id for t in past if t.occurred_at < window_start}
    count = 0
    for t in past:
        if t.occurred_at < window_start:
            continue
        if t.transaction_type == "transfer" and t.counterparty_id not in known:
            count += 1
        known.add(t.counterparty_id)
    return count


def _is_near_limit(cash_out: Transaction, history: HistoryProvider, config: FeatureConfig) -> bool:
    limit = config.kyc_limits[history.wallet(cash_out.subject_wallet_id).kyc_tier]
    return limit - limit * config.near_limit_share <= cash_out.amount <= limit


def transaction_features(
    transaction: Transaction, history: HistoryProvider, now: datetime, *, config: FeatureConfig
) -> TransactionFeatures:
    """All features of `transaction` from history strictly before `now`, the simulation clock.

    Every window is measured back from `now` and includes its start; the wall clock is never read.
    """
    receiver_fan_in = received_from = agent_count = agent_amount = agent_near_limit = None
    match transaction.transaction_type:
        case "transfer":
            receiver_fan_in = _fan_in(transaction.counterparty_id, history, now)
            received_from = _received_from(
                transaction.subject_wallet_id, transaction.counterparty_id, history, now
            )
        case "cash_out":
            receiver_fan_in = _fan_in(transaction.subject_wallet_id, history, now)
    if transaction.transaction_type in ("cash_in", "cash_out"):
        at_agent = history.agent_transactions(
            transaction.counterparty_id, since=now - _DAY, before=now
        )
        cash_outs = [t for t in at_agent if t.transaction_type == "cash_out"]
        agent_count = len(cash_outs)
        agent_amount = sum(t.amount for t in cash_outs)
        agent_near_limit = sum(_is_near_limit(t, history, config) for t in cash_outs)
    share_of_balance_moved = None
    if transaction.direction == "out" and transaction.subject_balance_before > 0:
        share_of_balance_moved = transaction.amount / transaction.subject_balance_before
    sim_swap = history.last_sim_swap(transaction.subject_wallet_id, before=now)
    past = history.subject_history(transaction.subject_wallet_id, before=now).transactions
    limit = config.kyc_limits[history.wallet(transaction.subject_wallet_id).kyc_tier]
    return TransactionFeatures(
        subject_wallet=subject_wallet_features(
            transaction, history, now, min_history=config.min_history
        ),
        receiver_fan_in_24h=receiver_fan_in,
        received_from_counterparty_24h=received_from,
        agent_cash_out_count_24h=agent_count,
        agent_cash_out_amount_24h=agent_amount,
        agent_near_limit_cash_out_count_24h=agent_near_limit,
        seconds_since_sim_swap=None if sim_swap is None else (now - sim_swap).total_seconds(),
        share_of_balance_moved=share_of_balance_moved,
        device_age_seconds=_device_age(transaction, past, now),
        new_counterparty_transfers_1h=_new_counterparty_transfers(past, now),
        amount_to_kyc_limit=transaction.amount / limit,
    )
