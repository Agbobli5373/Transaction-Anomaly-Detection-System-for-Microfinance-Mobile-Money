"""Scoring a whole Transactions table in time order, the way the evaluation does."""

from datetime import UTC, datetime, timedelta

import pandas as pd
from scoring_helpers import FEATURES, RULES_CONFIG

from txrisk.scoring import load_rules_config, score_table

START = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


def _transactions(rows: list[dict[str, object]]) -> pd.DataFrame:
    base = {
        "direction": "out",
        "subject_wallet_id": "w-subject",
        "subject_balance_before": 1000.0,
        "channel": "ussd",
        "device_id": "D-own",
        "region": "Ashanti",
        "counterparty_wallet_id": None,
        "agent_id": None,
        "merchant_id": None,
        "biller_id": None,
    }
    frame = pd.DataFrame([{**base, **row} for row in rows])
    frame["occurred_at"] = pd.to_datetime(frame["occurred_at"], utc=True)
    return frame


def test_score_table_scores_each_transaction_from_the_history_before_it():
    routine = [
        {
            "transaction_id": f"T{d}",
            "occurred_at": START + d * DAY + timedelta(hours=14),
            "transaction_type": "transfer",
            "amount": 50.0,
            "counterparty_wallet_id": "w-friend",
        }
        for d in range(7)
    ]
    drain = {
        "transaction_id": "T-drain",
        "occurred_at": START + 7 * DAY + timedelta(hours=12),
        "transaction_type": "cash_out",
        "amount": 900.0,
        "agent_id": "A1",
    }
    # Listed out of time order, to show the table keeps its own row order.
    transactions = _transactions([drain, *routine])
    wallets = pd.DataFrame(
        {
            "wallet_id": ["w-subject", "w-friend"],
            "registered_at": pd.to_datetime([START - 30 * DAY] * 2, utc=True),
            "kyc_tier": ["minimum", "minimum"],
        }
    )
    sim_swaps = pd.DataFrame(
        {
            "wallet_id": ["w-subject"],
            "occurred_at": pd.to_datetime([START + 7 * DAY + timedelta(hours=10)], utc=True),
        }
    )

    scores = score_table(
        transactions,
        wallets,
        sim_swaps,
        features=FEATURES,
        rules=load_rules_config(RULES_CONFIG),
        score_from=pd.Timestamp(START + 3 * DAY),
    )

    assert scores["transaction_id"].tolist() == transactions["transaction_id"].tolist()
    by_id = scores.set_index("transaction_id")
    # Before score_from: replayed into the history, but not scored.
    assert by_id.loc[["T0", "T1", "T2"], "risk_score"].isna().all()
    assert (by_id.loc[["T3", "T4", "T5", "T6"], "risk_band"] == "Low").all()
    assert by_id.loc["T-drain", "risk_band"] == "High"
    assert by_id.loc["T-drain", "reason_codes"].split("|")[0] == "SIM_SWAP_DRAIN"
