"""How fraud comes to light outside the system: victims complain or dispute a charge."""

import numpy as np
import pandas as pd

from txrisk.generator.config import FraudScenario, GeneratorConfig


def _episodes(frauds: pd.DataFrame) -> pd.Series:
    """Which report would cover each fraudulent Transaction.

    One victim reports one whole episode. Mule fan-in has many victims, each reporting their
    own Transfer; the mule's Cash-outs have no victim of their own and come to light only with
    the first of those reports.
    """
    mule = frauds["scenario"] == FraudScenario.MULE_FAN_IN.value
    sender = mule & (frauds["transaction_type"] == "transfer")
    key = frauds["instance_id"].astype(str)
    return key.where(~sender, key + ":" + frauds["subject_wallet_id"].astype(str))


def discover(
    config: GeneratorConfig, frauds: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Complaint and chargeback Fraud discoveries for settled fraudulent Transactions.

    `frauds` has one row per fraudulent Transaction: transaction_id, scenario, instance_id,
    subject_wallet_id, transaction_type and occurred_at.
    """
    params = config.fraud.discovery
    frauds = frauds.assign(episode=_episodes(frauds))
    mule_cash_out = (frauds["scenario"] == FraudScenario.MULE_FAN_IN.value) & (
        frauds["transaction_type"] == "cash_out"
    )

    reports: dict[str, tuple[pd.Timestamp, str]] = {}
    for episode, group in frauds[~mule_cash_out].groupby("episode", sort=True):
        scenario = FraudScenario(group["scenario"].iloc[0])
        if rng.random() >= params.probability[scenario]:
            continue
        delay = pd.Timedelta(days=float(rng.uniform(params.delay_days.low, params.delay_days.high)))
        source = "chargeback" if rng.random() < params.chargeback_share else "complaint"
        reports[str(episode)] = (group["occurred_at"].max() + delay, source)

    rows: list[dict[str, object]] = []
    for _, row in frauds[~mule_cash_out].iterrows():
        if row["episode"] in reports:
            at, source = reports[row["episode"]]
            rows.append(
                {"transaction_id": row["transaction_id"], "discovered_at": at, "source": source}
            )

    # A mule's Cash-outs surface with the first sender's report, but never before they happen.
    senders = frauds[~mule_cash_out & (frauds["scenario"] == FraudScenario.MULE_FAN_IN.value)]
    for instance, cash_outs in frauds[mule_cash_out].groupby("instance_id", sort=True):
        found = [
            reports[e]
            for e in senders.loc[senders["instance_id"] == instance, "episode"]
            if e in reports
        ]
        if not found:
            continue
        first_at, source = min(found)
        at = max(
            first_at, cash_outs["occurred_at"].max() + pd.Timedelta(days=params.delay_days.low)
        )
        rows += [
            {"transaction_id": t, "discovered_at": at, "source": source}
            for t in cash_outs["transaction_id"]
        ]

    found_frame = pd.DataFrame(rows, columns=["transaction_id", "discovered_at", "source"])
    found_frame["discovered_at"] = pd.to_datetime(found_frame["discovered_at"], utc=True)
    return found_frame.sort_values(["discovered_at", "transaction_id"], ignore_index=True).astype(
        {"transaction_id": "string", "source": "string"}
    )
