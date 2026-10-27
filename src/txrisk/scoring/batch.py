"""Scoring a whole Transactions table by replaying it in time order."""

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from txrisk.features import FeatureConfig, replay_history, transactions_from_frame
from txrisk.scoring.config import RulesConfig
from txrisk.scoring.service import ScoringService


@dataclass
class ReplayClock:
    """The simulation clock during a replay: it reads whatever moment the replay has reached."""

    at: datetime

    def now(self) -> datetime:
        return self.at


def score_table(
    transactions: pd.DataFrame,
    wallets: pd.DataFrame,
    sim_swaps: pd.DataFrame,
    *,
    features: FeatureConfig,
    rules: RulesConfig,
    score_from: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """A Risk assessment for every Transaction at or after `score_from`, in the input's order.

    Every Transaction is replayed into the history, but only those from `score_from` on are
    scored; earlier rows have empty scores. Each is scored with the clock at its own occurred-at,
    before it joins the history.
    """
    history = replay_history(wallets, sim_swaps)
    clock = ReplayClock(datetime.min)
    service = ScoringService(history, clock, features=features, rules=rules)
    chronological = transactions.sort_values("occurred_at", kind="stable")
    rows: list[tuple[object, ...]] = []
    for transaction in transactions_from_frame(chronological):
        if score_from is None or transaction.occurred_at >= score_from:
            clock.at = transaction.occurred_at
            assessment = service.assess(transaction)
            rows.append(
                (
                    assessment.risk_score,
                    assessment.risk_band.value,
                    assessment.component_scores["rules"],
                    "|".join(r.code for r in assessment.reasons),
                )
            )
        else:
            rows.append((None, None, None, None))
        history.record(transaction)

    scores = pd.DataFrame(
        rows,
        columns=["risk_score", "risk_band", "rules_score", "reason_codes"],
        index=chronological.index,
    ).astype(
        {
            "risk_score": "Int64",
            "risk_band": "string",
            "rules_score": "Float64",
            "reason_codes": "string",
        }
    )
    scores.insert(0, "transaction_id", chronological["transaction_id"])
    return scores.loc[transactions.index].reset_index(drop=True)
