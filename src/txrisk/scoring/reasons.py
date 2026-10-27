"""Reasons: why a Transaction got its Risk score, in a few stable codes and plain words.

Rules-only scoring gives its Rule hits, heaviest first. A stacked Risk score adds the model
inputs that pushed it up most, each in words with a stable code. An input that measures what a
rule that fired measures joins that rule's Reason, so one fact is never given twice.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from txrisk.scoring.config import RulesConfig, Severity
from txrisk.scoring.rules import RuleHit


@dataclass(frozen=True)
class Reason:
    code: str
    message: str
    value: float | str


def rule_reasons(hits: list[RuleHit], config: RulesConfig) -> tuple[Reason, ...]:
    """Critical hits first, then the heaviest; rule-set order breaks ties."""
    weight = config.severity_weights
    ranked = sorted(
        enumerate(hits),
        key=lambda h: (h[1].severity != Severity.CRITICAL, -weight[h[1].severity], h[0]),
    )
    return tuple(Reason(h.code, h.message, h.value) for _, h in ranked[: config.max_reasons])


def _duration(seconds: float) -> str:
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 86_400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86_400:.1f} days"


@dataclass(frozen=True)
class InputReason:
    """How one model input reads as a Reason.

    `describe` gives the message and value, or None when the input's value is no reason for
    alarm whatever the model made of it. Messages state the value without judging it: the model
    may have been pushed up by a long history as well as a short one. `same_as` names the rules
    that test what this input measures; when one of them fired, the input's push joins that
    rule's Reason instead of repeating the same fact under its own code.
    """

    code: str
    describe: Callable[[float], tuple[str, float] | None]
    same_as: tuple[str, ...] = ()


def _when_true(code: str, message: str, same_as: tuple[str, ...] = ()) -> InputReason:
    return InputReason(code, lambda v: (message, 1.0) if v >= 0.5 else None, same_as)


def _always(
    code: str, message: Callable[[float], str], same_as: tuple[str, ...] = ()
) -> InputReason:
    return InputReason(code, lambda v: (message(v), v), same_as)


def _count(
    code: str, message: Callable[[float], str], same_as: tuple[str, ...] = ()
) -> InputReason:
    """A count of events: none of them is never a reason for alarm."""
    return InputReason(code, lambda v: (message(v), v) if v > 0 else None, same_as)


_ODD_HOUR = ("ODD_HOUR",)
_DRAIN = ("SIM_SWAP_DRAIN",)
_BURST = ("NEW_DEVICE_BURST",)

INPUT_REASONS: dict[str, InputReason] = {
    "history_count": _always(
        "HISTORY_LENGTH", lambda v: f"{v:.0f} earlier Transactions on this Wallet"
    ),
    "wallet_age_days": _always("WALLET_AGE", lambda v: f"Wallet registered {v:.0f} days ago"),
    "velocity_count_1h": _count("VELOCITY_1H", lambda v: f"{v:.0f} Transactions in the last hour"),
    "velocity_amount_1h": _count("VELOCITY_1H", lambda v: f"{v:,.0f} GHS moved in the last hour"),
    "velocity_count_24h": _count(
        "VELOCITY_24H", lambda v: f"{v:.0f} Transactions in the last 24 hours"
    ),
    "velocity_amount_24h": _count(
        "VELOCITY_24H", lambda v: f"{v:,.0f} GHS moved in the last 24 hours"
    ),
    "velocity_count_7d": _count(
        "VELOCITY_7D", lambda v: f"{v:.0f} Transactions in the last 7 days"
    ),
    "velocity_amount_7d": _count("VELOCITY_7D", lambda v: f"{v:,.0f} GHS moved in the last 7 days"),
    "amount_to_median_30d": _always(
        "AMOUNT_VS_MEDIAN",
        lambda v: f"Amount is {v:.1f}x this Wallet's 30-day median",
        ("FIRST_TIME_LARGE_TRANSFER",),
    ),
    "seconds_since_last_transaction": _always(
        "TIME_SINCE_LAST", lambda v: f"{_duration(v)} since this Wallet's last Transaction"
    ),
    "new_counterparty": _when_true(
        "NEW_COUNTERPARTY",
        "First Transaction with this Counterparty",
        ("FIRST_TIME_LARGE_TRANSFER", "NEW_DEVICE_BURST"),
    ),
    "unusual_hour": _when_true(
        "UNUSUAL_HOUR", "At an hour this Wallet is rarely active", _ODD_HOUR
    ),
    "new_region_30d": _when_true("NEW_REGION", "First Transaction in this region in 30 days"),
    "night_share_30d": _always(
        "UNUSUAL_HOUR", lambda v: f"{v:.0%} of this Wallet's activity is at night", _ODD_HOUR
    ),
    "receiver_fan_in_24h": _count(
        "RECEIVER_FAN_IN",
        lambda v: f"The receiving Wallet got money from {v:.0f} different Wallets in 24 hours",
        ("MULE_CASHOUT",),
    ),
    "agent_cash_out_count_24h": _count(
        "AGENT_CASH_OUTS",
        lambda v: f"{v:.0f} Cash-outs at this Agent in 24 hours",
        ("AGENT_STRUCTURING",),
    ),
    "agent_cash_out_amount_24h": _count(
        "AGENT_CASH_OUTS",
        lambda v: f"{v:,.0f} GHS cashed out at this Agent in 24 hours",
        ("AGENT_STRUCTURING",),
    ),
    "agent_near_limit_cash_out_count_24h": _count(
        "AGENT_STRUCTURING",
        lambda v: f"{v:.0f} earlier Cash-outs just under the limit at this Agent in 24 hours",
    ),
    "seconds_since_sim_swap": _always(
        "RECENT_SIM_SWAP", lambda v: f"SIM swapped {_duration(v)} earlier", _DRAIN
    ),
    "share_of_balance_moved": _always(
        "SHARE_OF_BALANCE", lambda v: f"Moves {v:.0%} of the balance", _DRAIN
    ),
    "device_age_seconds": InputReason(
        "DEVICE_AGE",
        lambda v: (
            ("From a device this Wallet has never used", v)
            if v == 0
            else (f"From a device first used {_duration(v)} ago", v)
        ),
        _BURST,
    ),
    "new_counterparty_transfers_1h": _count(
        "NEW_COUNTERPARTY_BURST",
        lambda v: f"{v:.0f} Transfers to new Counterparties in the last hour",
        _BURST,
    ),
    "amount_to_kyc_limit": _always(
        "NEAR_KYC_LIMIT",
        lambda v: f"Amount is {v:.0%} of the KYC tier limit",
        ("AGENT_STRUCTURING",),
    ),
    "log_amount": InputReason(
        "AMOUNT",
        lambda v: (f"Amount of {math.expm1(v):,.0f} GHS", round(math.expm1(v), 2)),
    ),
    "hour_of_day": _always("UNUSUAL_HOUR", lambda v: f"At {v:02.0f}:00-{v:02.0f}:59", _ODD_HOUR),
}
# What kind of Transaction it is says nothing an analyst can act on, so these give no Reason.
UNEXPLAINED_INPUTS = {
    "is_app",
    "is_incoming",
    "type_cash_in",
    "type_cash_out",
    "type_transfer",
    "type_payment",
    "type_bill_payment",
}


@dataclass
class _Candidate:
    reason: Reason
    contribution: float
    critical: bool
    from_rule: bool


def stacked_reasons(
    hits: list[RuleHit],
    *,
    rules_contribution: float,
    inputs: pd.Series,
    contributions: pd.Series,
    config: RulesConfig,
) -> tuple[Reason, ...]:
    """The Reasons for one stacked Risk score: Critical rule hits first, then whatever pushed the
    score up most.

    `rules_contribution` is the rules' push on the Risk score's log-odds, shared among the Rule
    hits by severity weight; `contributions` is each model input's push, on the same scale, and
    `inputs` the values behind them. An input joins the Reason of a fired rule it overlaps.
    Candidates with one code become one Reason, worded as the rule words it if a rule fired, and
    counting their pushes together.
    """
    weights = config.severity_weights
    total_weight = sum(weights[h.severity] for h in hits)
    candidates = [
        _Candidate(
            Reason(h.code, h.message, h.value),
            rules_contribution * weights[h.severity] / total_weight,
            critical=h.severity == Severity.CRITICAL,
            from_rule=True,
        )
        for h in hits
    ]
    fired = {h.code for h in hits}
    for column, template in INPUT_REASONS.items():
        push, value = float(contributions[column]), float(inputs[column])
        if not push > 0 or math.isnan(value):  # NaN pushes fail `> 0` too
            continue
        described = template.describe(value)
        if described is not None:
            message, shown = described
            code = next((c for c in template.same_as if c in fired), template.code)
            reason = Reason(code, message, shown)
            candidates.append(_Candidate(reason, push, critical=False, from_rule=False))

    merged: dict[str, _Candidate] = {}
    for candidate in candidates:
        code = candidate.reason.code
        if code not in merged:
            merged[code] = candidate
            continue
        kept = merged[code]
        wording = (
            kept if kept.from_rule or kept.contribution >= candidate.contribution else candidate
        )
        merged[code] = _Candidate(
            wording.reason,
            kept.contribution + candidate.contribution,
            critical=kept.critical or candidate.critical,
            from_rule=kept.from_rule or candidate.from_rule,
        )
    ranked = sorted(
        (c for c in merged.values() if c.critical or c.contribution > 0),
        key=lambda c: (not c.critical, -c.contribution),
    )
    return tuple(c.reason for c in ranked[: config.max_reasons])
