"""The market spend index: how busy and how large the whole market's spending is right now.

A festive season is a Benign anomaly: Transactions are more numerous and each is bigger. Every
amount-driven score rises with it, so at a fixed High threshold the Alerts of a festive day double
while the fraud does not (#47). The index measures that market-wide surge from history alone, so
the High threshold can rise with it (ADR 0011).

Spend is the GHS moved by all Transactions of the market in each complete clock hour before the
Transaction. A recent window of the last 6 or 24 hours is compared with the same window on each of
the previous 7 days, which keeps the daily rhythm out of the ratio. The index is the larger of the
two ratios: the short window reacts within hours when a busy day starts, the long one fades slowly
when it ends.
"""

from datetime import datetime, timedelta

from txrisk.features.config import FeatureConfig
from txrisk.features.history import HistoryProvider

_HOUR = timedelta(hours=1)
_DAY_HOURS = 24
# Spend is kept to the cent, which is all a ledger holds, so that sums are the same whichever
# provider adds them up.
_CENTS = 2


def hour_start(moment: datetime) -> datetime:
    """The start of the clock hour `moment` falls in."""
    return moment.replace(minute=0, second=0, microsecond=0)


def market_spend_index(
    history: HistoryProvider, now: datetime, *, config: FeatureConfig
) -> float | None:
    """The market spend index at `now`, or None before the market has any history to compare.

    Only complete hours before `now`'s own hour are read, so the index never sees `now` itself.
    A baseline day with no spend in the window (before the data begins) is left out of the mean.
    """
    days = config.market_baseline_days
    end = hour_start(now)
    start = end - _HOUR * (_DAY_HOURS * days + max(config.market_windows_hours))
    spend = history.market_hourly_spend(since=start, before=end)
    last = len(spend)
    ratios = []
    for window in config.market_windows_hours:
        recent = sum(spend[last - window : last])
        earlier = [
            sum(spend[last - window - _DAY_HOURS * day : last - _DAY_HOURS * day])
            for day in range(1, days + 1)
        ]
        baseline = [total for total in earlier if total > 0]
        if baseline:
            ratios.append(recent / (sum(baseline) / len(baseline)))
    return round(max(ratios), 6) if ratios else None
