"""Parity: the API, scoring with a model bundle over Postgres, gives exactly the offline scores."""

from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import pytest
from api_helpers import as_json, body, settings
from conftest import create_database
from fastapi.testclient import TestClient
from market_helpers import CONFIG, RULES, START, Market, build_market, replay_assessments, train

from txrisk.api import REPLAYED_HEADER, create_app
from txrisk.evaluation import Split, split_of
from txrisk.features import Transaction, transactions_from_frame
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.models import model_inputs
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import rule_scores_table
from txrisk.store import create_engine, records, seed


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
    with TestClient(create_app(settings(whole_market, bundle_folder))) as client:
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
    url = _seeded(postgres, migrated_template, "parity_live", data, until_day=14)
    # SIM swaps from then on arrive from the operator as they happen, as they do offline.
    with create_engine(url).begin() as connection:
        later = market.sim_swaps[market.sim_swaps["occurred_at"] >= _day(14)]
        for row in later.itertuples():
            records.add_sim_swap(
                connection, row.wallet_id, row.occurred_at.floor("us").to_pydatetime()
            )
    live = market.transactions[market.transactions["occurred_at"] >= _day(14)]
    first = live.sort_values("occurred_at", kind="stable").head(60)
    ids = first["transaction_id"].tolist()
    expected = replay_assessments(market, bundle, set(ids))

    with TestClient(create_app(settings(url, bundle_folder))) as client:
        served = [client.post("/v1/score", json=body(t)).json() for t in _transactions(market, ids)]

    assert served == [as_json(expected[i]) for i in ids]
