"""Parity: the API and the stream workers, scoring with a model bundle over Postgres, give exactly
the offline scores, and so exactly each other's."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from api_helpers import ROOT, as_json, body, issue_key, settings, with_key
from confluent_kafka import Consumer
from conftest import create_database
from fastapi.testclient import TestClient
from market_helpers import CONFIG, RULES, START, Market, build_market, replay_assessments, train

from txrisk.api import REPLAYED_HEADER, create_app
from txrisk.cli import main
from txrisk.evaluation import Split, split_of
from txrisk.events import (
    Publisher,
    ScoreProduced,
    ScoringWorker,
    SimSwapRecorded,
    Topics,
    TransactionCreated,
    decode,
    ledger_events,
)
from txrisk.features import Transaction, transactions_from_frame
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.models import model_inputs
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import rule_scores_table
from txrisk.store import StoredScoring, create_engine, records, seed


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return train(market)


@pytest.fixture(scope="module")
def bundle_folder(bundle: ModelBundle, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The bundle as `txrisk train` saves it, for the API to load."""
    folder = tmp_path_factory.mktemp("models") / bundle.version
    bundle.save(folder)
    return folder


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("generated")
    write_dataset(market.dataset, folder)
    return folder


def _day(day: int) -> pd.Timestamp:
    return pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=day)


def _seeded(postgres: str, template: str, name: str, data: Path, *, until_day: int) -> str:
    url = create_database(postgres, name, template=template)
    engine = create_engine(url)
    seed(engine, data, until=_day(until_day).to_pydatetime())
    engine.dispose()
    return url


@pytest.fixture(scope="module")
def whole_market(data: Path, postgres: str, migrated_template: str) -> str:
    return _seeded(postgres, migrated_template, "parity_whole", data, until_day=21)


@pytest.fixture(scope="module")
def client(whole_market: str, bundle_folder: Path) -> Iterator[TestClient]:
    app = create_app(settings(whole_market, bundle_folder))
    with TestClient(app, headers=with_key(issue_key(whole_market))) as client:
        yield client


@pytest.fixture(scope="module")
def sample(market: Market, bundle: ModelBundle) -> list[str]:
    """Evaluation-day Transactions: the riskiest ten, every Critical rule hit and ten others."""
    split = split_of(market.transactions["occurred_at"], START, CONFIG)
    rows = (split == Split.EVALUATION).fillna(False).to_numpy(dtype=bool)
    transactions = market.transactions[rows]
    features = market.features[rows]
    rules = rule_scores_table(transactions, features, RULES)
    scores = bundle.risk_scores(model_inputs(transactions, features), rules)
    picked = {
        *scores.nlargest(10).index,
        *rules.index[rules["critical"]],
        *scores.sample(10, random_state=1).index,
    }
    # Rule and Risk scores are indexed by Transaction ID, which runs in event-time order.
    return sorted(picked)


def _transactions(market: Market, ids: list[str]) -> list[Transaction]:
    frame = market.transactions.set_index("transaction_id", drop=False).loc[ids]
    return transactions_from_frame(frame)


def test_the_api_serves_the_active_bundle(client: TestClient, bundle: ModelBundle):
    active = client.get("/v1/models/active").json()

    assert active["model_version"] == bundle.version
    assert active["thresholds"] == {
        "medium": bundle.thresholds.medium,
        "high": bundle.thresholds.high,
    }


def test_api_scores_equal_offline_scores_for_the_same_history(
    market: Market, bundle: ModelBundle, client: TestClient, sample: list[str]
):
    """Postgres holds every Transaction, later ones included; each is still scored against only
    the history before it, so the API agrees with the offline replay to the last digit."""
    expected = replay_assessments(market, bundle, set(sample))

    served = [client.post("/v1/score", json=body(t)).json() for t in _transactions(market, sample)]

    assert served == [as_json(expected[i]) for i in sample]
    assert any(s["risk_band"] == "High" for s in served)


def test_rescoring_replays_without_new_alerts(
    market: Market, client: TestClient, sample: list[str]
):
    transactions = _transactions(market, sample)
    first = [client.post("/v1/score", json=body(t)) for t in transactions]
    alerts = client.get("/v1/alerts", params={"limit": 500}).json()["alerts"]

    again = [client.post("/v1/score", json=body(t)) for t in transactions]

    assert [r.json() for r in again] == [r.json() for r in first]
    assert all(r.headers[REPLAYED_HEADER] == "true" for r in again)
    high = {r.json()["transaction_id"] for r in first if r.json()["risk_band"] == "High"}
    assert {a["transaction_id"] for a in alerts} == high
    assert client.get("/v1/alerts", params={"limit": 500}).json()["alerts"] == alerts


# The live Transactions scored one by one in the tests below: the first after day 14.
LIVE_FROM_DAY = 14
LIVE = 60


def _live_ids(market: Market) -> list[str]:
    live = market.transactions[market.transactions["occurred_at"] >= _day(LIVE_FROM_DAY)]
    return live.sort_values("occurred_at", kind="stable").head(LIVE)["transaction_id"].tolist()


def _scored_by_the_api(
    market: Market, ids: list[str], url: str, bundle_folder: Path
) -> list[dict[str, Any]]:
    """The API's answers for `ids`, posted in order to a database seeded up to the live days."""
    # SIM swaps from then on arrive from the operator as they happen, as they do offline.
    with create_engine(url).begin() as connection:
        later = market.sim_swaps[market.sim_swaps["occurred_at"] >= _day(LIVE_FROM_DAY)]
        for row in later.itertuples():
            records.add_sim_swap(
                connection, row.wallet_id, row.occurred_at.floor("us").to_pydatetime()
            )
    app = create_app(settings(url, bundle_folder))
    with TestClient(app, headers=with_key(issue_key(url))) as client:
        return [client.post("/v1/score", json=body(t)).json() for t in _transactions(market, ids)]


def test_scored_transactions_become_the_history_of_later_ones(
    market: Market,
    bundle: ModelBundle,
    bundle_folder: Path,
    data: Path,
    postgres: str,
    migrated_template: str,
):
    """Seeded up to the evaluation days, then scored one by one in event-time order: each scored
    Transaction is history for the next, exactly as in the offline replay."""
    url = _seeded(postgres, migrated_template, "parity_live", data, until_day=LIVE_FROM_DAY)
    ids = _live_ids(market)
    expected = replay_assessments(market, bundle, set(ids))

    served = _scored_by_the_api(market, ids, url, bundle_folder)

    assert served == [as_json(expected[i]) for i in ids]


def _read(bootstrap: str, topic: str, count: int) -> list[bytes]:
    consumer = Consumer(
        {"bootstrap.servers": bootstrap, "group.id": "parity", "auto.offset.reset": "earliest"}
    )
    consumer.subscribe([topic])
    found: list[bytes] = []
    deadline = time.monotonic() + 60
    while len(found) < count and time.monotonic() < deadline:
        message = consumer.poll(0.5)
        if message is not None and message.error() is None:
            found.append(message.value() or b"")
    consumer.close()
    return found


def test_the_stream_and_the_sync_api_give_identical_assessments(
    market: Market,
    bundle_folder: Path,
    data: Path,
    postgres: str,
    migrated_template: str,
    redpanda: str,
):
    """Both start from history seeded up to the live days. `txrisk produce` publishes the live
    SIM swaps and Transactions from Parquet and a worker scores them; the API gets the same SIM
    swaps loaded and the same Transactions posted in event-time order."""
    ids = _live_ids(market)
    streamed = _seeded(postgres, migrated_template, "stream_live", data, until_day=LIVE_FROM_DAY)
    topics = Topics("parity.")
    produced = main(
        [
            "produce",
            "--data",
            str(data),
            "--bootstrap-servers",
            redpanda,
            "--config",
            str(ROOT / "config" / "generator.toml"),
            "--from-day",
            str(LIVE_FROM_DAY),
            "--limit",
            str(LIVE),
            "--topic-prefix",
            topics.prefix,
            # One partition, so every history is consumed in event-time order, as the API gets it.
            "--partitions",
            "1",
        ]
    )
    published = _read(redpanda, topics.transactions, LIVE)
    assert produced == 0
    assert [decode(TransactionCreated, p).transaction_id for p in published] == ids

    worker_settings = settings(streamed, bundle_folder)
    worker = ScoringWorker(
        StoredScoring(
            create_engine(streamed),
            features=worker_settings.features(),
            rules=worker_settings.rules(),
            bundle=ModelBundle.load(bundle_folder),
        ),
        Publisher(redpanda),
        bootstrap_servers=redpanda,
        topics=topics,
        poll_timeout=0.5,
    )
    handled, deadline = 0, time.monotonic() + 120
    while handled < LIVE and time.monotonic() < deadline:
        handled += worker.run_batch()
    worker.close()
    scores = [decode(ScoreProduced, p) for p in _read(redpanda, topics.scores, LIVE)]
    by_id = {s.transaction_id: s.to_assessment() for s in scores}

    served = _seeded(postgres, migrated_template, "api_live", data, until_day=LIVE_FROM_DAY)
    answered = _scored_by_the_api(market, ids, served, bundle_folder)

    assert handled == len(scores) == LIVE
    assert [as_json(by_id[i]) for i in ids] == answered
    assert {s.model_version for s in scores} == {bundle_folder.name}


def test_the_ledger_interleaves_sim_swaps_with_transactions_in_event_time_order(
    market: Market, data: Path, tmp_path: Path
):
    """Only Transactions and SIM swaps are read: a dataset folder without Ground truth, Fraud
    discoveries or Benign anomalies is published just the same (ADR 0007)."""
    for name in ("transactions", "sim_swaps"):
        (tmp_path / f"{name}.parquet").write_bytes((data / f"{name}.parquet").read_bytes())
    since, until = _day(7), _day(10)

    events = list(ledger_events(tmp_path, since=since.to_pydatetime(), until=until.to_pydatetime()))

    times = [e.occurred_at for e in events]
    assert times == sorted(times)
    swaps = [e for e in events if isinstance(e, SimSwapRecorded)]
    sim_swaps, transactions = market.sim_swaps, market.transactions
    in_window = sim_swaps[(sim_swaps["occurred_at"] >= since) & (sim_swaps["occurred_at"] < until)]
    window = transactions[
        (transactions["occurred_at"] >= since) & (transactions["occurred_at"] < until)
    ]
    assert len(swaps) == len(in_window) > 0
    assert {e.transaction_id for e in events if isinstance(e, TransactionCreated)} == set(
        window["transaction_id"]
    )
