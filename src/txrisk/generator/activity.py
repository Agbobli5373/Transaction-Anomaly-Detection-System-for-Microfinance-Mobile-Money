"""Transactions of the Wallets in a Population: everyday activity plus injected fraud."""

from dataclasses import dataclass
from datetime import UTC, datetime, time

import numpy as np
import pandas as pd

from txrisk.generator.benign import (
    BENIGN,
    BenignAnomaly,
    Trips,
    benign_candidates,
    festive_multiples,
    plan_new_phones,
    plan_trips,
)
from txrisk.generator.config import FraudScenario, GeneratorConfig
from txrisk.generator.discovery import discover
from txrisk.generator.fraud import FraudContext, plan_fraud
from txrisk.generator.market import (
    CASH_IN,
    COUNTERPARTY_COLUMN,
    TRANSFER,
    TYPES,
    contacts,
    local_pick,
    members_by_region,
    region_codes,
    region_weights,
)
from txrisk.generator.population import Population

_AMOUNT_SPREAD = 0.8  # sigma of the log-normal amount distribution around each type's median
_MIN_AMOUNT = 100  # pesewas; smaller settled amounts are dropped as not worth transacting
_SCENARIOS = list(FraudScenario)


@dataclass(frozen=True)
class Activity:
    transactions: pd.DataFrame
    sim_swaps: pd.DataFrame
    ground_truth: pd.DataFrame
    fraud_discoveries: pd.DataFrame
    benign_anomalies: pd.DataFrame


def _everyday_candidates(
    config: GeneratorConfig,
    population: Population,
    wallet_contacts: np.ndarray,
    trips: Trips,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Intended legitimate Transactions before balances and limits are applied."""
    behaviour = config.behaviour
    type_weights = np.asarray([behaviour.transaction_types[t].share for t in TYPES])
    type_weights /= type_weights.sum()
    medians = np.asarray([behaviour.transaction_types[t].median_amount for t in TYPES])
    hour_weights = np.asarray(behaviour.hourly_activity) / sum(behaviour.hourly_activity)
    start = pd.Timestamp(datetime.combine(config.period.start, time(), tzinfo=UTC))
    n_wallets = len(population.wallets)
    regions = list(config.market.regions)
    weights = region_weights(config)
    home = region_codes(population.wallets["home_region"], regions)
    local = members_by_region(population, regions)

    days: list[pd.DataFrame] = []
    for day in range(config.period.days):
        day_start = start + pd.Timedelta(days=day)
        busier, larger = festive_multiples(config, day_start.date())
        rate = behaviour.mean_transactions_per_day * busier
        if day_start.day_of_week >= 5:
            rate *= behaviour.weekend_activity_factor
        subject = np.repeat(np.arange(n_wallets), rng.poisson(rate, n_wallets))
        n = len(subject)
        seconds = rng.choice(24, n, p=hour_weights) * 3600 + rng.integers(0, 3600, n)
        type_index = rng.choice(len(TYPES), n, p=type_weights)
        intended = medians[type_index] * larger * rng.lognormal(0.0, _AMOUNT_SPREAD, n)
        picks = wallet_contacts[subject, rng.integers(0, wallet_contacts.shape[1], n)]
        frame = pd.DataFrame(
            {
                "occurred_at": day_start + pd.to_timedelta(seconds, unit="s"),
                "subject": subject,
                "type_index": type_index,
                "intended_pesewas": np.round(intended * 100).astype(np.int64),
                "counterparty_wallet": picks,
            }
        )
        # Mostly where the Wallet holder lives; sometimes elsewhere.
        location = np.where(
            rng.random(n) < behaviour.home_region_share,
            home[subject],
            rng.choice(len(regions), n, p=weights),
        )
        # A Wallet holder away on a trip transacts where they are.
        away = trips.away(subject, frame["occurred_at"].dt.tz_convert(None).to_numpy())
        location = np.where(away, trips.region[subject], location)
        frame["location"] = location
        frame["benign"] = np.where(away, BENIGN.index(BenignAnomaly.TRAVEL), -1)
        for column, members in local.items():
            frame[column] = local_pick(location, members, rng)
        frame["biller_id"] = rng.integers(0, len(population.billers), n)
        days.append(frame)

    candidates = pd.concat(days, ignore_index=True)
    candidates["scenario"] = -1
    candidates["instance"] = -1
    candidates["device"] = None
    candidates["channel"] = None
    candidates["drain_share"] = np.nan
    candidates["require_exact"] = False
    return candidates


def _in_time_order(
    candidates: pd.DataFrame, registered: np.ndarray, period_end: np.datetime64
) -> pd.DataFrame:
    """Sort candidates and drop any that fall after the period or involve a Wallet that has not
    registered yet. A Fraud scenario that starts near the end is cut off at the end."""
    candidates = candidates.assign(benign=candidates["benign"].fillna(-1).astype(np.int64))
    candidates = candidates.sort_values(
        ["occurred_at", "subject"], kind="stable", ignore_index=True
    )
    occurred = candidates["occurred_at"].dt.tz_convert(None).to_numpy()
    is_transfer = candidates["type_index"].to_numpy() == TRANSFER
    registered_parties = (occurred >= registered[candidates["subject"].to_numpy()]) & (
        ~is_transfer | (occurred >= registered[candidates["counterparty_wallet"].to_numpy()])
    )
    in_period = occurred < period_end
    kept: pd.DataFrame = candidates[registered_parties & in_period].reset_index(drop=True)
    return kept


def _settle(
    candidates: pd.DataFrame,
    opening_balance: np.ndarray,
    single_limit: np.ndarray,
    max_balance: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Replay candidates against Wallet balances (pesewas).

    A candidate with a drain share intends that share of the Subject wallet's balance at that
    moment; one that requires an exact amount is dropped rather than settled for less. Returns
    the settled amount and the Subject wallet's balance before each candidate, plus a mask of the
    candidates that could be settled at all.
    """
    balance = opening_balance.astype(np.int64).tolist()
    limit = single_limit.tolist()
    ceiling = max_balance.tolist()
    subjects = candidates["subject"].tolist()
    types = candidates["type_index"].tolist()
    intended = candidates["intended_pesewas"].tolist()
    receivers = candidates["counterparty_wallet"].tolist()
    drains = candidates["drain_share"].tolist()
    exact = candidates["require_exact"].tolist()

    n = len(subjects)
    amounts = np.zeros(n, dtype=np.int64)
    balance_before = np.zeros(n, dtype=np.int64)
    settled = np.zeros(n, dtype=bool)
    for i in range(n):
        w, t, drain = subjects[i], types[i], drains[i]
        wanted = intended[i] if drain != drain else round(balance[w] * drain)  # NaN: no drain
        amount = min(wanted, limit[w])
        if t == CASH_IN:
            amount = min(amount, ceiling[w] - balance[w])
        else:
            amount = min(amount, balance[w])
            if t == TRANSFER:
                r = receivers[i]
                amount = min(amount, ceiling[r] - balance[r])
        if amount < _MIN_AMOUNT or (exact[i] and amount < wanted):
            continue
        settled[i] = True
        amounts[i] = amount
        balance_before[i] = balance[w]
        if t == CASH_IN:
            balance[w] += amount
        else:
            balance[w] -= amount
            if t == TRANSFER:
                balance[receivers[i]] += amount
    return amounts, balance_before, settled


def simulate(config: GeneratorConfig, population: Population, rng: np.random.Generator) -> Activity:
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
    wallet_contacts = contacts(len(wallets), config.population.contacts_per_wallet, rng)
    # Each Wallet holder keeps to one Channel and one device in normal behaviour.
    wallet_channel = np.where(
        rng.random(len(wallets)) < config.behaviour.usd_channel_share, "ussd", "app"
    )
    wallet_device = np.asarray([f"D{d:016x}" for d in rng.integers(0, 2**63 - 1, len(wallets))])

    period_end = (start + pd.Timedelta(days=config.period.days)).tz_convert(None).to_datetime64()

    # First pass: everyday activity alone, to learn which Wallets usually hold money.
    everyday = _in_time_order(
        pd.concat(
            [
                _everyday_candidates(
                    config, population, wallet_contacts, plan_trips(config, population, rng), rng
                ),
                benign_candidates(config, population, rng),
            ],
            ignore_index=True,
        ),
        registered,
        period_end,
    )
    _, everyday_before, everyday_settled = _settle(everyday, opening, single_limit, max_balance)
    usual_balance = (
        pd.Series(everyday_before[everyday_settled])
        .groupby(everyday["subject"].to_numpy()[everyday_settled])
        .median()
        .reindex(range(len(wallets)), fill_value=0)
        .to_numpy()
    )

    plan = plan_fraud(
        config,
        population,
        FraudContext(
            contacts=wallet_contacts,
            registered=registered,
            single_limit=single_limit,
            max_balance=max_balance,
            usual_balance=usual_balance,
            everyday_transactions=int(everyday_settled.sum()),
        ),
        rng,
    )

    # Second pass: everyday activity and fraud settle together against the same balances.
    candidates = _in_time_order(
        pd.concat([everyday, plan.candidates], ignore_index=True), registered, period_end
    )
    amounts, balance_before, settled = _settle(candidates, opening, single_limit, max_balance)
    tx = candidates[settled].reset_index(drop=True)
    type_index = tx["type_index"].to_numpy()
    subject = tx["subject"].to_numpy()

    # From a legitimate new phone onward, the holder uses it (and sometimes moves to the App).
    phones = plan_new_phones(config, population, rng)
    switched = tx["occurred_at"].dt.tz_convert(None).to_numpy() >= phones.switch_at[subject]
    device = np.where(switched, phones.device[subject], wallet_device[subject])
    channel = np.where(switched & phones.to_app[subject], "app", wallet_channel[subject])
    benign = tx["benign"].to_numpy().copy()
    own_phone = switched & tx["device"].isna().to_numpy()
    first_on_new_phone = own_phone & ~pd.Series(subject).where(own_phone).duplicated().to_numpy()
    benign[first_on_new_phone] = BENIGN.index(BenignAnomaly.NEW_PHONE)

    wallet_ids = wallets["wallet_id"].to_numpy()
    regions = np.asarray(list(config.market.regions))
    region = regions[tx["location"].to_numpy()]
    for column, table in (("agent_id", population.agents), ("merchant_id", population.merchants)):
        at_counterparty = np.isin(
            type_index, [i for i, t in enumerate(TYPES) if COUNTERPARTY_COLUMN[t] == column]
        )
        counterparty_region = table["region"].to_numpy()[tx[column].to_numpy()]
        region = np.where(at_counterparty, counterparty_region, region)

    transactions = pd.DataFrame(
        {
            "transaction_id": [f"T{i:09d}" for i in range(len(tx))],
            "occurred_at": tx["occurred_at"],
            "transaction_type": np.asarray([t.value for t in TYPES])[type_index],
            "amount": amounts[settled] / 100,
            "subject_wallet_id": wallet_ids[subject],
            "direction": np.where(type_index == CASH_IN, "in", "out"),
            "subject_balance_before": balance_before[settled] / 100,
            "channel": tx["channel"].fillna(pd.Series(channel)).to_numpy(),
            "device_id": tx["device"].fillna(pd.Series(device)).to_numpy(),
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
            type_index, [i for i, t in enumerate(TYPES) if COUNTERPARTY_COLUMN[t] == column]
        )
        transactions[column] = pd.Series(
            np.where(uses, known[picks.to_numpy()], None), dtype="string"
        )
    for column in ("transaction_type", "direction", "channel", "device_id", "region"):
        transactions[column] = transactions[column].astype("string")

    fraudulent = tx["scenario"].to_numpy() >= 0
    ground_truth = pd.DataFrame(
        {
            "transaction_id": transactions["transaction_id"][fraudulent].to_numpy(),
            "scenario": np.asarray([s.value for s in _SCENARIOS] + [""])[tx["scenario"].to_numpy()][
                fraudulent
            ],
            "instance_id": tx["instance"].to_numpy()[fraudulent],
        }
    ).astype({"transaction_id": "string", "scenario": "string", "instance_id": "int64"})
    sim_swaps = pd.DataFrame(
        {
            "wallet_id": pd.Series(wallet_ids[plan.sim_swaps["wallet"].to_numpy()], dtype="string"),
            "occurred_at": plan.sim_swaps["occurred_at"].to_numpy(),
        }
    )
    sim_swaps["occurred_at"] = pd.to_datetime(sim_swaps["occurred_at"], utc=True)
    unusual = benign >= 0
    benign_anomalies = pd.DataFrame(
        {
            "transaction_id": transactions["transaction_id"][unusual].to_numpy(),
            "kind": np.asarray([b.value for b in BENIGN])[benign[unusual]],
        }
    ).astype({"transaction_id": "string", "kind": "string"})
    fraud_discoveries = discover(
        config,
        ground_truth.merge(
            transactions[
                ["transaction_id", "subject_wallet_id", "transaction_type", "occurred_at"]
            ],
            on="transaction_id",
        ),
        rng,
    )
    return Activity(
        transactions=transactions,
        sim_swaps=sim_swaps,
        ground_truth=ground_truth,
        fraud_discoveries=fraud_discoveries,
        benign_anomalies=benign_anomalies,
    )
