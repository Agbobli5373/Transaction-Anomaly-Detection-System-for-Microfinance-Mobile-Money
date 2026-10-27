"""Legitimate day-to-day Transactions of the Wallets in a Population."""

from datetime import UTC, datetime, time

import numpy as np
import pandas as pd

from txrisk.generator.config import GeneratorConfig, TransactionType
from txrisk.generator.population import Population

_AMOUNT_SPREAD = 0.8  # sigma of the log-normal amount distribution around each type's median
_MIN_AMOUNT = 100  # pesewas; smaller settled amounts are dropped as not worth transacting
_TYPES = list(TransactionType)
_CASH_IN = _TYPES.index(TransactionType.CASH_IN)
_TRANSFER = _TYPES.index(TransactionType.TRANSFER)
_COUNTERPARTY_COLUMN = {
    TransactionType.CASH_IN: "agent_id",
    TransactionType.CASH_OUT: "agent_id",
    TransactionType.TRANSFER: "counterparty_wallet_id",
    TransactionType.PAYMENT: "merchant_id",
    TransactionType.BILL_PAYMENT: "biller_id",
}


def _contacts(n_wallets: int, per_wallet: int, rng: np.random.Generator) -> np.ndarray:
    """Each Wallet's circle of other Wallets it Transfers to, never including itself."""
    contacts = rng.integers(0, n_wallets - 1, (n_wallets, per_wallet))
    return contacts + (contacts >= np.arange(n_wallets)[:, None])


def _local_pick(
    location: np.ndarray, members_by_region: list[np.ndarray], total: int, rng: np.random.Generator
) -> np.ndarray:
    """A Counterparty in the Transaction's region, or anywhere if that region has none."""
    picks = rng.integers(0, total, len(location))
    for region, members in enumerate(members_by_region):
        here = location == region
        if members.size and here.any():
            picks[here] = members[rng.integers(0, members.size, int(here.sum()))]
    return picks


def _region_codes(values: pd.Series, regions: list[str]) -> np.ndarray:
    return np.asarray(pd.Categorical(values, categories=regions).codes)


def _candidates(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> pd.DataFrame:
    """Intended Transactions before balances and limits are applied, in time order."""
    behaviour = config.behaviour
    type_weights = np.asarray([behaviour.transaction_types[t].share for t in _TYPES])
    type_weights /= type_weights.sum()
    medians = np.asarray([behaviour.transaction_types[t].median_amount for t in _TYPES])
    hour_weights = np.asarray(behaviour.hourly_activity) / sum(behaviour.hourly_activity)
    start = pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))
    n_wallets = len(population.wallets)
    regions = list(config.market.regions)
    region_weights = np.asarray(list(config.market.regions.values()))
    region_weights /= region_weights.sum()
    home = _region_codes(population.wallets["home_region"], regions)
    local = {
        "agent_id": _region_codes(population.agents["region"], regions),
        "merchant_id": _region_codes(population.merchants["region"], regions),
    }
    members_by_region = {
        column: [np.flatnonzero(codes == r) for r in range(len(regions))]
        for column, codes in local.items()
    }
    contacts = _contacts(n_wallets, config.population.contacts_per_wallet, rng)

    days: list[pd.DataFrame] = []
    for day in range(config.period.days):
        day_start = start + pd.Timedelta(days=day)
        rate = behaviour.mean_transactions_per_day
        if day_start.day_of_week >= 5:
            rate *= behaviour.weekend_activity_factor
        subject = np.repeat(np.arange(n_wallets), rng.poisson(rate, n_wallets))
        n = len(subject)
        seconds = rng.choice(24, n, p=hour_weights) * 3600 + rng.integers(0, 3600, n)
        type_index = rng.choice(len(_TYPES), n, p=type_weights)
        intended = medians[type_index] * rng.lognormal(0.0, _AMOUNT_SPREAD, n)
        frame = pd.DataFrame(
            {
                "occurred_at": day_start + pd.to_timedelta(seconds, unit="s"),
                "subject": subject,
                "type_index": type_index,
                "intended_pesewas": np.round(intended * 100).astype(np.int64),
                "counterparty_wallet": contacts[subject, rng.integers(0, contacts.shape[1], n)],
            }
        )
        # Mostly where the Wallet holder lives; sometimes elsewhere.
        location = np.where(
            rng.random(n) < behaviour.home_region_share,
            home[subject],
            rng.choice(len(regions), n, p=region_weights),
        )
        frame["location"] = location
        for column, codes in local.items():
            frame[column] = _local_pick(location, members_by_region[column], len(codes), rng)
        frame["biller_id"] = rng.integers(0, len(population.billers), n)
        days.append(frame)

    candidates = pd.concat(days, ignore_index=True)
    return candidates.sort_values(["occurred_at", "subject"], kind="stable", ignore_index=True)


def _settle(
    candidates: pd.DataFrame,
    opening_balance: np.ndarray,
    single_limit: np.ndarray,
    max_balance: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Replay candidates against Wallet balances (pesewas).

    Returns the settled amount and the Subject wallet's balance before each candidate, plus a
    mask of the candidates that could be settled at all.
    """
    balance = opening_balance.astype(np.int64).tolist()
    limit = single_limit.tolist()
    ceiling = max_balance.tolist()
    subjects = candidates["subject"].tolist()
    types = candidates["type_index"].tolist()
    intended = candidates["intended_pesewas"].tolist()
    receivers = candidates["counterparty_wallet"].tolist()

    n = len(subjects)
    amounts = np.zeros(n, dtype=np.int64)
    balance_before = np.zeros(n, dtype=np.int64)
    settled = np.zeros(n, dtype=bool)
    for i in range(n):
        w, t = subjects[i], types[i]
        amount = min(intended[i], limit[w])
        if t == _CASH_IN:
            amount = min(amount, ceiling[w] - balance[w])
        else:
            amount = min(amount, balance[w])
            if t == _TRANSFER:
                r = receivers[i]
                amount = min(amount, ceiling[r] - balance[r])
        if amount < _MIN_AMOUNT:
            continue
        settled[i] = True
        amounts[i] = amount
        balance_before[i] = balance[w]
        if t == _CASH_IN:
            balance[w] += amount
        else:
            balance[w] -= amount
            if t == _TRANSFER:
                balance[receivers[i]] += amount
    return amounts, balance_before, settled


def simulate_transactions(
    config: GeneratorConfig, population: Population, rng: np.random.Generator
) -> pd.DataFrame:
    wallets = population.wallets
    tiers = {t.name: t for t in config.market.kyc_tiers}
    single_limit = np.asarray(
        [tiers[k].single_transaction_limit * 100 for k in wallets["kyc_tier"]]
    )
    max_balance = np.asarray([tiers[k].max_balance * 100 for k in wallets["kyc_tier"]])
    start = pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))
    registered = wallets["registered_at"].dt.tz_convert(None).to_numpy()
    # Wallets that already exist hold some money; Wallets registered during the period start empty.
    opening = np.where(
        registered < start.tz_convert(None).to_datetime64(),
        np.minimum(
            np.round(
                config.behaviour.median_opening_balance
                * 100
                * rng.lognormal(0.0, 1.0, len(wallets))
            ),
            max_balance,
        ),
        0,
    )

    candidates = _candidates(config, population, rng)
    occurred = candidates["occurred_at"].dt.tz_convert(None).to_numpy()
    is_transfer = candidates["type_index"].to_numpy() == _TRANSFER
    registered_parties = (occurred >= registered[candidates["subject"].to_numpy()]) & (
        ~is_transfer | (occurred >= registered[candidates["counterparty_wallet"].to_numpy()])
    )
    candidates = candidates[registered_parties].reset_index(drop=True)
    amounts, balance_before, settled = _settle(candidates, opening, single_limit, max_balance)
    tx = candidates[settled].reset_index(drop=True)
    type_index = tx["type_index"].to_numpy()

    wallet_ids = wallets["wallet_id"].to_numpy()
    subject = tx["subject"].to_numpy()
    # Each Wallet holder keeps to one Channel and one device in normal behaviour.
    wallet_channel = np.where(
        rng.random(len(wallets)) < config.behaviour.usd_channel_share, "ussd", "app"
    )
    wallet_device = np.asarray([f"D{d:016x}" for d in rng.integers(0, 2**63 - 1, len(wallets))])
    regions = np.asarray(list(config.market.regions))
    region = regions[tx["location"].to_numpy()]
    for column, table in (("agent_id", population.agents), ("merchant_id", population.merchants)):
        at_counterparty = np.isin(
            type_index, [i for i, t in enumerate(_TYPES) if _COUNTERPARTY_COLUMN[t] == column]
        )
        counterparty_region = table["region"].to_numpy()[tx[column].to_numpy()]
        region = np.where(at_counterparty, counterparty_region, region)

    transactions = pd.DataFrame(
        {
            "transaction_id": [f"T{i:09d}" for i in range(len(tx))],
            "occurred_at": tx["occurred_at"],
            "transaction_type": np.asarray([t.value for t in _TYPES])[type_index],
            "amount": amounts[settled] / 100,
            "subject_wallet_id": wallet_ids[subject],
            "direction": np.where(type_index == _CASH_IN, "in", "out"),
            "subject_balance_before": balance_before[settled] / 100,
            "channel": wallet_channel[subject],
            "device_id": wallet_device[subject],
            "region": region,
        }
    )
    ids = {
        "counterparty_wallet_id": (wallet_ids, tx["counterparty_wallet"]),
        "agent_id": (population.agents["agent_id"].to_numpy(), tx["agent_id"]),
        "merchant_id": (population.merchants["merchant_id"].to_numpy(), tx["merchant_id"]),
        "biller_id": (population.billers["biller_id"].to_numpy(), tx["biller_id"]),
    }
    for column, (known, picks) in ids.items():
        uses = np.isin(
            type_index, [i for i, t in enumerate(_TYPES) if _COUNTERPARTY_COLUMN[t] == column]
        )
        transactions[column] = pd.Series(
            np.where(uses, known[picks.to_numpy()], None), dtype="string"
        )
    return transactions
