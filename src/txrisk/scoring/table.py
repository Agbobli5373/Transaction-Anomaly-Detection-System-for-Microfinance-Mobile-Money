"""Rule scores for a whole table at once, from its feature table rather than a replay."""

import pandas as pd

from txrisk.features import features_from_frame, transactions_from_frame
from txrisk.scoring.config import RulesConfig
from txrisk.scoring.rules import rule_hits
from txrisk.scoring.service import combine_hits


def rule_scores_table(
    transactions: pd.DataFrame, feature_rows: pd.DataFrame, rules: RulesConfig
) -> pd.DataFrame:
    """The rules score, Critical flag and Rule hit codes of each Transaction, indexed by
    transaction_id in `transactions` order.

    The features already hold everything the rules read, so this gives the scoring service's
    answer without replaying history again.
    """
    aligned = (
        transactions[["transaction_id"]]
        .merge(feature_rows, on="transaction_id", how="left")
    )
    scores, critical, codes = [], [], []
    for transaction, features in zip(
        transactions_from_frame(transactions), features_from_frame(aligned), strict=True
    ):
        hits = rule_hits(transaction, features, rules.rules)
        score, is_critical = combine_hits(hits, rules)
        scores.append(score)
        critical.append(is_critical)
        codes.append("|".join(h.code for h in hits))
    return pd.DataFrame(
        {"rules_score": scores, "critical": critical, "rule_codes": codes},
        index=pd.Index(transactions["transaction_id"], name="transaction_id"),
    )
