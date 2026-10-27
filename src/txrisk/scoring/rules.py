"""The rule set: deterministic, backward-looking checks of a Transaction and its features."""

from collections.abc import Callable
from dataclasses import dataclass

from txrisk.features import Transaction, TransactionFeatures
from txrisk.scoring.config import Rules, Severity

_HOUR = 3600.0


@dataclass(frozen=True)
class RuleHit:
    code: str
    severity: Severity
    message: str
    value: float | str


# A check returns the Reason's message and value when its rule fires, otherwise None.
_Check = Callable[[Transaction, TransactionFeatures, Rules], tuple[str, float | str] | None]


def _sim_swap_drain(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    params = rules.SIM_SWAP_DRAIN
    since, share = f.seconds_since_sim_swap, f.share_of_balance_moved
    if t.transaction_type not in ("cash_out", "transfer") or since is None or share is None:
        return None
    if since > params.within_hours * _HOUR or share < params.min_share_of_balance:
        return None
    return f"Moved {share:.0%} of the balance {since / _HOUR:.1f} hours after a SIM swap", share


def _new_device_burst(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    params = rules.NEW_DEVICE_BURST
    new_counterparty = f.subject_wallet.new_counterparty
    if t.transaction_type != "transfer" or new_counterparty is None:  # None: cold start
        return None
    burst = f.new_counterparty_transfers_1h + int(new_counterparty)
    if f.device_age_seconds > params.device_new_within_minutes * 60:
        return None
    if burst < params.min_new_counterparty_transfers:
        return None
    minutes = f.device_age_seconds / 60
    return (
        f"{burst} Transfers to new Counterparties within an hour, from a device first used "
        f"{minutes:.0f} minutes ago",
        burst,
    )


def _mule_cash_out(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    senders = f.receiver_fan_in_24h
    if t.transaction_type != "cash_out" or senders is None:
        return None
    if senders < rules.MULE_CASHOUT.min_distinct_senders_24h:
        return None
    return f"Cash-out after receiving from {senders} different Wallets in 24 hours", senders


def _agent_structuring(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    params = rules.AGENT_STRUCTURING
    earlier = f.agent_near_limit_cash_out_count_24h
    if t.transaction_type != "cash_out" or earlier is None:
        return None
    if f.amount_to_kyc_limit < params.min_share_of_limit:
        return None
    count = earlier + 1  # this Cash-out is near the limit too
    if count < params.min_near_limit_cash_outs_24h:
        return None
    return f"{count} Cash-outs just under the limit at this Agent in 24 hours", count


def _first_time_large_transfer(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    ratio = f.subject_wallet.amount_to_median_30d
    if t.transaction_type != "transfer" or not f.subject_wallet.new_counterparty or ratio is None:
        return None
    if ratio <= rules.FIRST_TIME_LARGE_TRANSFER.above_median_multiple:
        return None
    return f"Transfer of {ratio:.1f}x the usual amount to a first-time recipient", ratio


def _amount_vs_median(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    ratio = f.subject_wallet.amount_to_median_30d
    if t.direction != "out" or ratio is None:
        return None
    if ratio <= rules.AMOUNT_VS_MEDIAN.above_median_multiple:
        return None
    return f"Amount is {ratio:.1f}x this Wallet's 30-day median", ratio


def _near_kyc_limit(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    share = f.amount_to_kyc_limit
    if share < rules.NEAR_KYC_LIMIT.min_share_of_limit:
        return None
    return f"Amount is {share:.0%} of the KYC tier limit", share


def _new_region(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    if not f.subject_wallet.new_region_30d:  # also None in cold start
        return None
    return f"First Transaction in {t.region} in 30 days", t.region


def _odd_hour(
    t: Transaction, f: TransactionFeatures, rules: Rules
) -> tuple[str, float | str] | None:
    params = rules.ODD_HOUR
    share = f.subject_wallet.night_share_30d
    if t.occurred_at.hour >= params.night_ends_at_hour or share is None:
        return None
    if share >= params.max_night_share:
        return None
    return (
        f"At {t.occurred_at:%H:%M}, when {share:.0%} of this Wallet's activity is at night",
        share,
    )


# The rule set in a fixed order, which also breaks ties between Reasons of equal weight.
RULES: dict[str, _Check] = {
    "SIM_SWAP_DRAIN": _sim_swap_drain,
    "NEW_DEVICE_BURST": _new_device_burst,
    "MULE_CASHOUT": _mule_cash_out,
    "AGENT_STRUCTURING": _agent_structuring,
    "FIRST_TIME_LARGE_TRANSFER": _first_time_large_transfer,
    "AMOUNT_VS_MEDIAN": _amount_vs_median,
    "NEAR_KYC_LIMIT": _near_kyc_limit,
    "NEW_REGION": _new_region,
    "ODD_HOUR": _odd_hour,
}


def rule_hits(
    transaction: Transaction, features: TransactionFeatures, rules: Rules
) -> list[RuleHit]:
    """Every rule that fires for this Transaction, in rule-set order. Disabled rules never do."""
    hits: list[RuleHit] = []
    for code, check in RULES.items():
        if code in rules.disabled:
            continue
        fired = check(transaction, features, rules)
        if fired is not None:
            message, value = fired
            severity = getattr(rules, code).severity
            hits.append(RuleHit(code=code, severity=severity, message=message, value=value))
    return hits
