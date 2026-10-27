"""Features describing a Transaction against its Subject wallet's Profile."""

import bisect
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from txrisk.features.history import HistoryProvider, Transaction

_DAY = timedelta(days=1)
# Below this many prior Transactions, history-dependent features are left empty.
DEFAULT_MIN_HISTORY = 5
# How far back the Profile reaches for the median amount, hours of day and regions.
_PROFILE_WINDOW = timedelta(days=30)
# Night is from midnight up to, but not including, 05:00.
_NIGHT_ENDS_AT_HOUR = 5


@dataclass(frozen=True)
class SubjectWalletFeatures:
    """History count and Wallet age are always present; the rest are None in cold start."""

    history_count: int
    wallet_age_days: float
    velocity_count_1h: int | None = None
    velocity_amount_1h: float | None = None
    velocity_count_24h: int | None = None
    velocity_amount_24h: float | None = None
    velocity_count_7d: int | None = None
    velocity_amount_7d: float | None = None
    amount_to_median_30d: float | None = None
    seconds_since_last_transaction: float | None = None
    new_counterparty: bool | None = None
    unusual_hour: bool | None = None
    new_region_30d: bool | None = None
    # Share of the last 30 days' Transactions made between 00:00 and 05:00.
    night_share_30d: float | None = None


def _since(transactions: Sequence[Transaction], start: datetime) -> Sequence[Transaction]:
    """The Transactions at or after `start`, from a time-ordered sequence."""
    return transactions[bisect.bisect_left(transactions, start, key=lambda t: t.occurred_at) :]


def subject_wallet_features(
    transaction: Transaction,
    history: HistoryProvider,
    now: datetime,
    *,
    min_history: int = DEFAULT_MIN_HISTORY,
) -> SubjectWalletFeatures:
    """Features of `transaction` against its Subject wallet's history strictly before `now`.

    `now` is the simulation clock reading when the Transaction is scored (its occurred-at in
    replay); every window is measured back from it, and the wall clock is never read.
    """
    subject = history.subject_history(transaction.subject_wallet_id, before=now)
    past = subject.transactions
    history_count = len(past)
    wallet_age_days = (now - subject.registered_at) / _DAY
    if history_count < min_history:
        return SubjectWalletFeatures(history_count=history_count, wallet_age_days=wallet_age_days)

    last_hour = _since(past, now - timedelta(hours=1))
    last_day = _since(past, now - _DAY)
    last_week = _since(past, now - 7 * _DAY)
    profile = _since(past, now - _PROFILE_WINDOW)
    outgoing = [t.amount for t in profile if t.direction == "out"]
    hour = transaction.occurred_at.hour
    nearby_hours = {(hour + offset) % 24 for offset in (-1, 0, 1)}
    return SubjectWalletFeatures(
        history_count=history_count,
        wallet_age_days=wallet_age_days,
        velocity_count_1h=len(last_hour),
        velocity_amount_1h=sum(t.amount for t in last_hour),
        velocity_count_24h=len(last_day),
        velocity_amount_24h=sum(t.amount for t in last_day),
        velocity_count_7d=len(last_week),
        velocity_amount_7d=sum(t.amount for t in last_week),
        amount_to_median_30d=transaction.amount / statistics.median(outgoing) if outgoing else None,
        seconds_since_last_transaction=(now - past[-1].occurred_at).total_seconds(),
        new_counterparty=all(t.counterparty_id != transaction.counterparty_id for t in past),
        unusual_hour=all(t.occurred_at.hour not in nearby_hours for t in profile),
        new_region_30d=all(t.region != transaction.region for t in profile),
        night_share_30d=(
            sum(t.occurred_at.hour < _NIGHT_ENDS_AT_HOUR for t in profile) / len(profile)
            if profile
            else None
        ),
    )
