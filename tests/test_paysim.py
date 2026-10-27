"""`txrisk paysim`: the PaySim benchmark maps what PaySim has, leaves out what it lacks, and
reports like the main evaluation."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from scoring_helpers import FEATURES as SERVICE_FEATURES
from scoring_helpers import HOUR, NOW, Market, routine, tx

from txrisk.benchmark import (
    SUPPORTED_RULES,
    TYPE_MAPPING,
    UNSUPPORTED_FEATURES,
    UNSUPPORTED_RULES,
    load_paysim,
    paysim_features,
    paysim_rules,
)
from txrisk.benchmark.paysim import paysim_feature_config
from txrisk.cli import main
from txrisk.evaluation import load_evaluation_config
from txrisk.scoring import RULES, FixedClock, RulesConfig, ScoringService, load_rules_config
from txrisk.scoring.config import Rules

REPO = Path(__file__).resolve().parents[1]
COLUMNS = [
    "step", "type", "amount", "nameOrig", "oldbalanceOrg", "newbalanceOrig",
    "nameDest", "oldbalanceDest", "newbalanceDest", "isFraud", "isFlaggedFraud",
]  # fmt: skip
# PaySim columns that must never matter, because they follow the Transaction or label the dest.
NEVER_READ = ["newbalanceOrig", "oldbalanceDest", "newbalanceDest", "isFlaggedFraud"]


def hand_built() -> pd.DataFrame:
    """One Transaction of each PaySim type, in PaySim's own column order and spelling."""
    rows = [
        (1, "CASH_IN", 100.0, "C1", 50.0, 150.0, "C100", 0.0, 0.0, 0, 0),
        (2, "CASH_OUT", 40.0, "C2", 90.0, 50.0, "C101", 5.0, 45.0, 0, 0),
        (3, "TRANSFER", 30.0, "C3", 30.0, 0.0, "C4", 7.0, 37.0, 1, 0),
        (25, "PAYMENT", 20.0, "C5", 80.0, 60.0, "M200", 0.0, 0.0, 0, 0),
        (26, "DEBIT", 10.0, "C6", 60.0, 50.0, "C102", 1.0, 11.0, 0, 0),
    ]
    return pd.DataFrame(rows, columns=COLUMNS)


def paysim_market(poison: bool = False) -> pd.DataFrame:
    """About 5,000 ordinary Transactions over 16 days, plus 120 frauds that each Transfer a
    victim's whole balance to a mule, which Cashes out the same hour. With `poison`, the columns
    that must never be read hold values that give the labels away."""
    rng = np.random.default_rng(3)
    customers = [f"C{1000 + i}" for i in range(300)]
    types = ["CASH_IN", "CASH_OUT", "TRANSFER", "PAYMENT", "DEBIT"]
    rows = []
    for _ in range(5000):
        kind = str(rng.choice(types, p=[0.2, 0.3, 0.25, 0.22, 0.03]))
        amount = float(rng.lognormal(4, 1))
        balance = amount * float(rng.uniform(1, 20))
        dest = f"M{rng.integers(30)}" if kind == "PAYMENT" else str(rng.choice(customers))
        rows.append(
            (int(rng.integers(1, 16 * 24 + 1)), kind, amount, str(rng.choice(customers)),
             balance, balance - amount, dest, 0.0, amount, 0, 0)
        )  # fmt: skip
    for k in range(60):
        step = int(rng.integers(1, 16 * 24))
        amount = float(rng.uniform(5000, 20000))
        victim, mule = str(rng.choice(customers)), f"C9{k % 20:03d}"
        rows.append((step, "TRANSFER", amount, victim, amount, 0.0, mule, 0.0, amount, 1, 0))
        rows.append((step, "CASH_OUT", amount, mule, amount, 0.0, "C1", 0.0, amount, 1, 0))
    frame = pd.DataFrame(rows, columns=COLUMNS).sort_values("step", kind="stable")
    if poison:
        fraud = frame["isFraud"] == 1
        frame["newbalanceOrig"] = np.where(fraud, 0.0, 1e6)
        frame["oldbalanceDest"] = np.where(fraud, 1e9, 0.0)
        frame["newbalanceDest"] = np.where(fraud, 1e9 + 1, 1.0)
        frame["isFlaggedFraud"] = fraud.astype(int)
    return frame.reset_index(drop=True)


def write_csv(frame: pd.DataFrame, path: Path) -> Path:
    frame.to_csv(path, index=False)
    return path


FEATURES = paysim_feature_config(REPO / "config" / "features.toml")


# The mapping.


def test_paysim_types_map_onto_our_transaction_types_directions_and_counterparties(
    tmp_path: Path,
):
    paysim = load_paysim(write_csv(hand_built(), tmp_path / "ps.csv"))
    tx_ = paysim.transactions.set_index("paysim_type")

    assert {k: v.transaction_type for k, v in TYPE_MAPPING.items()} == {
        "CASH_IN": "cash_in",
        "CASH_OUT": "cash_out",
        "TRANSFER": "transfer",
        "PAYMENT": "payment",
        "DEBIT": "cash_out",
    }
    assert list(tx_["transaction_type"]) == [
        "cash_in",
        "cash_out",
        "transfer",
        "payment",
        "cash_out",
    ]
    assert list(tx_["direction"]) == ["in", "out", "out", "out", "out"]
    # nameDest is the Counterparty, in the column of its kind.
    assert tx_.loc["TRANSFER", "counterparty_wallet_id"] == "C4"
    assert tx_.loc["PAYMENT", "merchant_id"] == "M200"
    assert tx_.loc["CASH_IN", "agent_id"] == "C100"
    assert pd.isna(tx_.loc["TRANSFER", "agent_id"])
    assert tx_["biller_id"].isna().all()
    # nameOrig is the Subject wallet and oldbalanceOrg its balance before.
    assert list(tx_["subject_wallet_id"]) == ["C1", "C2", "C3", "C5", "C6"]
    assert list(tx_["subject_balance_before"]) == [50.0, 90.0, 30.0, 80.0, 60.0]
    assert list(tx_["amount"]) == [100.0, 40.0, 30.0, 20.0, 10.0]


def test_a_step_is_an_hour_and_step_one_is_midnight_of_the_first_day(tmp_path: Path):
    paysim = load_paysim(write_csv(hand_built(), tmp_path / "ps.csv"))

    at = paysim.transactions["occurred_at"]
    assert at.iloc[0] == pd.Timestamp("2026-01-01 00:00", tz="UTC")
    assert at.iloc[3] == pd.Timestamp("2026-01-02 00:00", tz="UTC")  # step 25


def test_a_paysim_type_with_no_mapping_is_refused_rather_than_guessed(tmp_path: Path):
    frame = hand_built()
    frame.loc[0, "type"] = "REFUND"

    with pytest.raises(ValueError, match="REFUND"):
        load_paysim(write_csv(frame, tmp_path / "ps.csv"))


def test_the_labels_are_complete_so_every_fraud_is_known_when_it_happens(tmp_path: Path):
    paysim = load_paysim(write_csv(hand_built(), tmp_path / "ps.csv"))

    fraud = paysim.transactions[paysim.transactions["paysim_type"] == "TRANSFER"]
    assert list(paysim.truth["transaction_id"]) == list(fraud["transaction_id"])
    assert list(paysim.discoveries["discovered_at"]) == list(fraud["occurred_at"])
    assert "isFraud" not in paysim.transactions.columns  # labels never ride along


# Balance-after fields.


def test_the_balance_after_and_destination_balance_columns_are_never_read(tmp_path: Path):
    clean = paysim_market()
    poisoned = paysim_market(poison=True)
    assert not clean[NEVER_READ].equals(poisoned[NEVER_READ])

    full = load_paysim(write_csv(poisoned, tmp_path / "poisoned.csv"))
    reference = load_paysim(write_csv(clean, tmp_path / "clean.csv"))
    without = load_paysim(write_csv(clean.drop(columns=NEVER_READ), tmp_path / "without.csv"))

    for other in (full, without):
        pd.testing.assert_frame_equal(other.transactions, reference.transactions)
        pd.testing.assert_frame_equal(other.truth, reference.truth)
        pd.testing.assert_frame_equal(other.discoveries, reference.discoveries)
    feature_rows = paysim_features(reference, FEATURES)
    pd.testing.assert_frame_equal(paysim_features(full, FEATURES), feature_rows)


# Features.


def test_features_use_only_what_happened_before_each_transaction(tmp_path: Path):
    market = paysim_market()
    cut = market[market["step"] <= 200]
    full = paysim_features(load_paysim(write_csv(market, tmp_path / "full.csv")), FEATURES)
    early = paysim_features(load_paysim(write_csv(cut, tmp_path / "early.csv")), FEATURES)

    # The first rows are the same Transactions, so later history must not have changed them.
    pd.testing.assert_frame_equal(full.iloc[: len(early)], early)


def test_transactions_in_the_same_hour_cannot_see_each_other(tmp_path: Path):
    rows = [
        (5, "TRANSFER", 10.0, "C1", 100.0, 90.0, "C9", 0.0, 10.0, 0, 0),
        (5, "TRANSFER", 10.0, "C2", 100.0, 90.0, "C9", 0.0, 10.0, 0, 0),
        (6, "TRANSFER", 10.0, "C3", 100.0, 90.0, "C9", 0.0, 10.0, 0, 0),
    ]
    paysim = load_paysim(write_csv(pd.DataFrame(rows, columns=COLUMNS), tmp_path / "ps.csv"))

    features = paysim_features(paysim, FEATURES)

    assert list(features["receiver_fan_in_24h"]) == [0, 0, 2]


def test_features_that_need_data_paysim_lacks_are_empty_and_the_rest_are_not(tmp_path: Path):
    paysim = load_paysim(write_csv(paysim_market(), tmp_path / "ps.csv"))

    features = paysim_features(paysim, FEATURES)

    for column in UNSUPPORTED_FEATURES:
        assert features[column].isna().all(), column
    for column in ("receiver_fan_in_24h", "share_of_balance_moved", "velocity_count_24h"):
        assert features[column].notna().any(), column
    assert features["receiver_fan_in_24h"].max() >= 2  # the mules


# Rules.


def test_every_rule_is_either_supported_or_explicitly_left_out():
    assert set(SUPPORTED_RULES) | set(UNSUPPORTED_RULES) == set(RULES)
    assert not set(SUPPORTED_RULES) & set(UNSUPPORTED_RULES)


def test_a_rule_paysim_cannot_feed_never_fires_but_the_others_still_do():
    base = load_rules_config(REPO / "config" / "rules.toml")
    market = Market().record(*routine()).sim_swap(NOW - HOUR)
    big = tx(transaction_type="cash_out", amount=900.0)

    def codes(rules: RulesConfig) -> list[str]:
        service = ScoringService(
            market.history, FixedClock(NOW), features=SERVICE_FEATURES, rules=rules
        )
        return [r.code for r in service.assess(big).reasons]

    assert "SIM_SWAP_DRAIN" in codes(base)
    left = codes(paysim_rules(base))
    assert "SIM_SWAP_DRAIN" not in left
    assert "AMOUNT_VS_MEDIAN" in left
    assert base.rules.disabled == frozenset()  # the real rule set is untouched


def test_only_rules_that_exist_can_be_disabled():
    rules = load_rules_config(REPO / "config" / "rules.toml").rules

    with pytest.raises(ValidationError, match="NOT_A_RULE"):
        Rules.model_validate({**rules.model_dump(), "disabled": ["NOT_A_RULE"]})


# The command, end to end.


def _evaluation_config(path: Path) -> Path:
    path.write_text(
        "label_maturity_days = 0\nalert_budget_per_day = 10\nwallet_age_band_edges = [30, 180]\n"
        "[splits]\ntrain = [0, 6]\ncalibration = [6, 8]\nthresholds = [8, 12]\n"
        "evaluation = [12, 16]\n",
        encoding="utf-8",
    )
    return path


def _paysim(root: Path, csv: Path, out: Path, *options: str) -> None:
    exit_code = main(
        [
            "paysim",
            "--csv", str(csv),
            "--evaluation-config", str(_evaluation_config(root / "paysim.toml")),
            "--feature-config", str(REPO / "config" / "features.toml"),
            "--rules-config", str(REPO / "config" / "rules.toml"),
            "--out", str(out),
            *options,
        ]
    )  # fmt: skip
    assert exit_code == 0


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("paysim")
    csv = write_csv(paysim_market(), root / "PS_log.csv")
    _paysim(root, csv, root / "report", "--features", str(root / "features.parquet"))
    return root


def _metrics(run: Path, scorer: str) -> dict:
    return json.loads((run / "report" / scorer / "metrics.json").read_text(encoding="utf-8"))


def test_every_scorer_gets_a_report_like_the_main_evaluations(run: Path):
    for scorer in ("rules-only", "anomaly", "supervised", "stacked"):
        report = run / "report" / scorer
        assert {p.name for p in report.iterdir()} >= {"metrics.json", "scores.parquet"}
        assert {
            "scorer", "split", "days", "transactions", "fraud", "fraud_discovered", "ranking",
            "at_high", "at_medium_or_higher", "at_alert_budget", "recall_by_scenario",
            "recall_against", "segments", "benchmark",
        } <= set(_metrics(run, scorer))  # fmt: skip
    assert "reliability" in _metrics(run, "stacked")
    assert (run / "features.parquet").exists()


def test_the_report_covers_exactly_the_evaluation_days(run: Path):
    metrics = _metrics(run, "stacked")
    market = paysim_market()
    evaluated = market[market["step"] > 12 * 24]

    assert metrics["days"] == [12, 16]
    assert metrics["transactions"] == len(evaluated)
    assert metrics["fraud"] == evaluated["isFraud"].sum()
    # Labels are complete, so what the operation knows is everything that happened.
    assert metrics["fraud_discovered"] == metrics["fraud"]
    assert (
        metrics["recall_against"]["ground_truth"] == metrics["recall_against"]["discovered_labels"]
    )


def test_the_report_says_what_was_left_out_and_why(run: Path):
    benchmark = _metrics(run, "stacked")["benchmark"]

    assert benchmark["unsupported_features"] == UNSUPPORTED_FEATURES
    assert benchmark["unsupported_rules"] == UNSUPPORTED_RULES
    assert benchmark["supported_rules"] == SUPPORTED_RULES
    assert benchmark["mapping"]["DEBIT"] == "cash_out"


def test_results_are_grouped_by_paysim_type_not_by_invented_segments(run: Path):
    metrics = _metrics(run, "stacked")

    assert set(metrics["segments"]) == {"paysim_type"}
    assert set(metrics["segments"]["paysim_type"]) <= set(TYPE_MAPPING)
    assert set(metrics["recall_by_scenario"]) == {"TRANSFER", "CASH_OUT"}


def test_high_is_set_at_the_alert_budget_on_the_threshold_days(run: Path):
    metrics = _metrics(run, "supervised")

    assert metrics["at_alert_budget"]["alerts"] == 10 * 4
    assert 5 <= metrics["at_high"]["per_day"] <= 40
    assert metrics["ranking"]["roc_auc"] > 0.9  # whole-balance drains are easy to see


def test_the_unused_columns_cannot_change_a_report(run: Path, tmp_path: Path):
    poisoned = write_csv(paysim_market(poison=True), tmp_path / "poisoned.csv")

    _paysim(tmp_path, poisoned, tmp_path / "report", "--scorer", "stacked")

    clean = run / "report" / "stacked"
    assert (tmp_path / "report" / "stacked" / "metrics.json").read_text() == (
        clean / "metrics.json"
    ).read_text()
    pd.testing.assert_frame_equal(
        pd.read_parquet(tmp_path / "report" / "stacked" / "scores.parquet"),
        pd.read_parquet(clean / "scores.parquet"),
    )


def test_the_default_split_is_ours_cut_proportionally():
    ours = load_evaluation_config(REPO / "config" / "evaluation.toml").splits
    theirs = load_evaluation_config(REPO / "config" / "paysim.toml").splits

    for name in ("train", "calibration", "thresholds", "evaluation"):
        ours_share = (getattr(ours, name).end - getattr(ours, name).start) / 120
        theirs_share = (getattr(theirs, name).end - getattr(theirs, name).start) / 32
        assert theirs_share == pytest.approx(ours_share)
