"""The fast scoring path gives the assessments the slow one gave.

Serving scores a Transaction through a numpy row and, for a stream worker, scores a whole batch
together against history read in bulk. Both are held here to what scoring one Transaction at a
time through data frames, one query at a time, gives: the same Risk band, the same Reasons in the
same order, and Risk scores and components that agree to float precision.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import redis
import sqlalchemy as sa
from api_helpers import as_app_role, settings
from conftest import create_database
from degraded_helpers import AGENT, STRANGER, drain, on_app, populate
from market_helpers import RULES, START, Market, build_market, train
from scoring_helpers import DAY, HOUR, NOW, SUBJECT, routine, tx

from txrisk.features import (
    ReplayHistory,
    Transaction,
    replay_history,
    transaction_features,
    transactions_from_frame,
)
from txrisk.features.market import hour_start
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.models import INPUT_COLUMNS, input_row, inputs_for, model_inputs
from txrisk.models.bundle import ModelBundle
from txrisk.scoring import FixedClock, RiskAssessment, ScoringService
from txrisk.store import (
    APP_ROLE,
    PostgresHistory,
    RedisHistoryCache,
    Scored,
    SimulationClock,
    StoredScoring,
    create_engine,
    records,
    seed,
)
from txrisk.store.batch_history import batch_history


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture(scope="module")
def bundle(market: Market) -> ModelBundle:
    return train(market)


class FrameOnly:
    """A bundle seen through its data frame API alone, which is how serving scored before the
    row API: a `ScoringService` over it takes the slow path."""

    scores_rows = False

    def __init__(self, bundle: ModelBundle) -> None:
        self._bundle = bundle

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bundle, name)


def _chronological(market: Market) -> list[Transaction]:
    return transactions_from_frame(market.transactions.sort_values("occurred_at", kind="stable"))


def _replayed(
    market: Market, take: Callable[[Transaction, ReplayHistory], None], last: int
) -> None:
    """Hand `take` each of the market's last `last` Transactions with the replayed history before
    it, then add it to the history."""
    history = replay_history(market.wallets, market.sim_swaps)
    chronological = _chronological(market)
    for i, transaction in enumerate(chronological):
        if i >= len(chronological) - last:
            take(transaction, history)
        history.record(transaction)


def assert_same_assessment(fast: RiskAssessment, slow: RiskAssessment) -> None:
    """Equal in everything that is exact, and to float precision in the Risk score and the
    components, which sum the same terms in another order."""
    assert fast.transaction_id == slow.transaction_id
    assert fast.risk_band == slow.risk_band
    assert fast.reasons == slow.reasons  # codes, messages, values and their order
    assert fast.model_version == slow.model_version
    assert fast.degraded == slow.degraded
    assert fast.risk_score == pytest.approx(slow.risk_score, abs=0.01)
    assert fast.component_scores.keys() == slow.component_scores.keys()
    for name, value in fast.component_scores.items():
        expected = slow.component_scores[name]
        assert (value is None) == (expected is None)
        if value is not None and expected is not None:
            assert value == pytest.approx(expected, rel=1e-9, abs=1e-12)
    if slow.model_inputs is None:
        assert fast.model_inputs is None
    else:
        assert fast.model_inputs is not None
        assert fast.model_inputs.keys() == slow.model_inputs.keys()
        for name, value in fast.model_inputs.items():
            np.testing.assert_array_equal(value, slow.model_inputs[name])


# The model path: numpy rows against data frames.


def test_a_numpy_row_of_model_inputs_is_the_data_frame_row(market: Market):
    checked = []

    def compare(transaction: Transaction, history: ReplayHistory) -> None:
        features = transaction_features(
            transaction,
            history,
            now=transaction.occurred_at,
            config=market.feature_config,
        )
        frame = inputs_for(transaction, features).iloc[0]
        assert list(frame.index) == INPUT_COLUMNS
        np.testing.assert_array_equal(input_row(transaction, features), frame.to_numpy())
        checked.append(transaction)

    _replayed(market, compare, last=400)

    assert len(checked) == 400
    assert {t.transaction_type for t in checked} >= {"transfer", "cash_out", "payment"}


def test_the_forest_walked_for_all_trees_at_once_scores_as_scikit_learn_does(
    market: Market, bundle: ModelBundle
):
    inputs = model_inputs(market.transactions, market.features).iloc[-1500:]
    matrix = inputs[INPUT_COLUMNS].fillna(-1.0).to_numpy(dtype=np.float64)
    model = bundle.anomaly
    unusualness = -model.estimator.score_samples(matrix)
    expected = np.searchsorted(model.training_unusualness, unusualness, side="right") / len(
        model.training_unusualness
    )

    scored = model.score(inputs).to_numpy()

    np.testing.assert_array_equal(scored, expected)
    # One row at a time, as serving scores it, gives what the whole matrix does.
    one_by_one = [model.score_matrix(row[None, :])[0] for row in inputs.to_numpy()[:100]]
    np.testing.assert_array_equal(one_by_one, expected[:100])


def test_the_row_path_gives_the_assessments_of_the_data_frame_path(
    market: Market, bundle: ModelBundle
):
    compared = []

    def compare(transaction: Transaction, history: ReplayHistory) -> None:
        def service(model: Any) -> ScoringService:
            return ScoringService(
                history,
                FixedClock(transaction.occurred_at),
                features=market.feature_config,
                rules=RULES,
                bundle=model,
            )

        fast = service(bundle).assess(transaction)
        slow = service(FrameOnly(bundle)).assess(transaction)
        assert_same_assessment(fast, slow)
        compared.append(fast)

    _replayed(market, compare, last=300)

    assert not any(a.degraded for a in compared)
    assert {a.risk_band.value for a in compared} >= {"Low"}
    assert any(a.reasons for a in compared)


def test_a_batch_of_rows_gives_the_assessments_of_one_row_at_a_time(
    market: Market, bundle: ModelBundle
):
    """`assess_batch` over a replayed history, recording each Transaction as its features are
    computed, against `assess` with every earlier Transaction already in the history."""
    last = _chronological(market)[-120:]
    first = last[0]
    one_by_one = replay_history(market.wallets, market.sim_swaps)
    for t in _chronological(market)[:-120]:
        one_by_one.record(t)
    batched = replay_history(market.wallets, market.sim_swaps)
    for t in _chronological(market)[:-120]:
        batched.record(t)

    expected = []
    for t in last:
        expected.append(
            ScoringService(
                one_by_one,
                FixedClock(t.occurred_at),
                features=market.feature_config,
                rules=RULES,
                bundle=bundle,
            ).assess(t)
        )
        one_by_one.record(t)
    service = ScoringService(
        batched,
        FixedClock(first.occurred_at),
        features=market.feature_config,
        rules=RULES,
        bundle=bundle,
    )
    scored = service.assess_batch(last, None, record=batched.record)

    assert len(scored) == len(expected)
    for (assessment, shadow), slow in zip(scored, expected, strict=True):
        assert shadow is None
        assert_same_assessment(assessment, slow)


# The store path: a batch against one at a time, in Postgres.


@pytest.fixture(scope="module")
def data(market: Market, tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("generated")
    write_dataset(market.dataset, folder)
    return folder


_CUTOFF_DAY = 14


def _seeded(postgres: str, template: str, name: str, data: Path) -> str:
    url = create_database(postgres, name, template=template)
    engine = create_engine(url)
    cutoff = pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=_CUTOFF_DAY)
    seed(engine, data, until=cutoff.to_pydatetime())
    engine.dispose()
    return url


def _live(market: Market, count: int) -> list[Transaction]:
    """The first `count` Transactions from the cutoff on, in event-time order."""
    cutoff = pd.Timestamp(START, tz="UTC") + pd.Timedelta(days=_CUTOFF_DAY)
    frame = market.transactions[market.transactions["occurred_at"] >= cutoff]
    frame = frame.sort_values(["occurred_at", "transaction_id"], kind="stable").head(count)
    return transactions_from_frame(frame.assign(occurred_at=frame["occurred_at"].dt.floor("us")))


def _scoring(
    database: str, bundle: ModelBundle | None, cache: RedisHistoryCache | None = None
) -> StoredScoring:
    api = settings(database)
    return StoredScoring(
        create_engine(as_app_role(database)),
        features=api.features(),
        rules=api.rules(),
        bundle=bundle,
        cache=cache,
    )


def _rows(database: str, query: str) -> list[Any]:
    engine = create_engine(database)
    with engine.connect() as connection:
        rows = [tuple(row) for row in connection.execute(sa.text(query))]
    engine.dispose()
    return rows


@pytest.fixture(scope="module")
def pair(postgres: str, migrated_template: str, data: Path) -> tuple[str, str]:
    """Two databases holding the same history up to the cutoff."""
    return (
        _seeded(postgres, migrated_template, "fast_one_by_one", data),
        _seeded(postgres, migrated_template, "fast_batched", data),
    )


def test_scoring_a_batch_stores_what_scoring_one_by_one_stores(
    market: Market, bundle: ModelBundle, pair: tuple[str, str]
):
    one_by_one_db, batched_db = pair
    live = _live(market, 360)
    one_by_one = _scoring(one_by_one_db, bundle)
    batched = _scoring(batched_db, bundle)

    expected = [one_by_one.assess(t) for t in live]
    outcomes: list[Scored | Exception] = []
    for start in range(0, len(live), 90):
        outcomes += batched.assess_many(live[start : start + 90])

    assert len(outcomes) == len(expected)
    for outcome, slow in zip(outcomes, expected, strict=True):
        assert isinstance(outcome, Scored)
        assert outcome.replayed == slow.replayed is False
        assert_same_assessment(outcome.assessment, slow.assessment)
    assert any(o.assessment.risk_band.value != "Low" for o in outcomes if isinstance(o, Scored))
    # Storage is the same: Transactions, assessments, model inputs, Alerts and the market's spend.
    for query in (
        "SELECT count(*) FROM transactions",
        "SELECT transaction_id, risk_band, model_version, degraded FROM risk_assessments"
        " ORDER BY transaction_id",
        "SELECT transaction_id, inputs FROM model_inputs ORDER BY transaction_id",
        "SELECT transaction_id, subject_wallet_id, raised_at FROM alerts ORDER BY transaction_id",
        "SELECT hour, sum(amount) FROM market_hours GROUP BY hour ORDER BY hour",
    ):
        assert _rows(batched_db, query) == _rows(one_by_one_db, query), query
    assert _rows(batched_db, "SELECT count(*) FROM alerts")[0][0] > 0


def test_a_batch_keeps_the_history_cache_as_one_by_one_does(
    market: Market,
    bundle: ModelBundle,
    postgres: str,
    migrated_template: str,
    data: Path,
    redis_url: str,
):
    live = _live(market, 150)
    caches = []
    for name, index in (("fast_cache_one", 1), ("fast_cache_many", 2)):
        client = redis.Redis.from_url(redis_url.rsplit("/", 1)[0] + f"/{index}")
        client.flushdb()
        cache = RedisHistoryCache(client)
        scoring = _scoring(_seeded(postgres, migrated_template, name, data), bundle, cache)
        if index == 1:
            for t in live:
                scoring.assess(t)
        else:
            for start in range(0, len(live), 50):
                scoring.assess_many(live[start : start + 50])
        caches.append(client)
    one, many = caches

    def contents(client: redis.Redis) -> dict[bytes, list[Any]]:
        found = {}
        for key in client.scan_iter("txrisk:history:*"):
            if key.endswith(b":from"):
                found[key] = [client.get(key)]
            else:
                found[key] = client.zrange(key, 0, -1, withscores=True)
        return found

    # Every set the batches loaded holds what the one-by-one run's does.
    assert contents(many).keys() <= contents(one).keys()
    one_sets = contents(one)
    for key, held in contents(many).items():
        assert held == one_sets[key], key


# A hand-built world, as in the stream and Degraded mode tests.


@pytest.fixture
def world(database: str) -> str:
    populate(database)
    engine = create_engine(database)
    with engine.begin() as connection:
        records.add_sim_swap(connection, SUBJECT, NOW - 2 * HOUR)
    engine.dispose()
    return database


def test_a_batch_sees_its_earlier_transactions_and_never_itself_or_later_ones(world: str):
    config = settings(world).features()
    first, second, third = (on_app(tx(NOW + i * HOUR, amount=40.0 + i)) for i in range(3))
    engine = create_engine(as_app_role(world))

    def history_counts(batch: list[Transaction]) -> dict[str, int]:
        with engine.connect() as connection:
            history = batch_history(connection, batch, config=config)
            counts = {}
            for t in batch:
                features = transaction_features(t, history, now=t.occurred_at, config=config)
                counts[t.transaction_id] = features.subject_wallet.history_count
                history.record(t)
        return counts

    routine_length = len(routine())
    # In time order, each sees the routine and the batch's Transactions before it.
    assert history_counts([first, second, third]) == {
        first.transaction_id: routine_length,
        second.transaction_id: routine_length + 1,
        third.transaction_id: routine_length + 2,
    }
    # An earlier Transaction that comes later in the batch is not seen by the later one that
    # came first, as when stored one by one in that order, and sees none of the later ones.
    assert history_counts([third, first, second]) == {
        third.transaction_id: routine_length,
        first.transaction_id: routine_length,
        second.transaction_id: routine_length + 1,
    }
    # Two at the very same instant do not see each other.
    twin = replace(first, transaction_id="T-twin")
    assert history_counts([first, twin]) == {
        first.transaction_id: routine_length,
        twin.transaction_id: routine_length,
    }
    engine.dispose()


def test_a_batch_adds_its_earlier_transactions_to_the_markets_spend(world: str):
    config = settings(world).features()
    start = hour_start(NOW)
    earlier = on_app(tx(start + 10 * timedelta(minutes=1), amount=40.0))
    later = on_app(tx(start + 2 * HOUR + timedelta(minutes=5), amount=41.0))
    engine = create_engine(as_app_role(world))
    with engine.connect() as connection:
        committed = PostgresHistory(connection).market_hourly_spend(start, start + 2 * HOUR)
        history = batch_history(connection, [earlier, later], config=config)
        assert history.market_hourly_spend(start, start + 2 * HOUR) == committed
        history.record(earlier)
        spend = history.market_hourly_spend(start, start + 2 * HOUR)
    assert spend == [round(committed[0] + earlier.amount, 2), committed[1]]
    engine.dispose()


def test_a_batch_answers_replays_conflicts_and_unknown_parties_as_scoring_one_by_one_does(
    world: str,
):
    scoring = _scoring(world, None)
    usual = on_app(tx(NOW, amount=40.0))
    repeated = replace(usual)
    conflicting = replace(usual, amount=41.0)
    unknown = on_app(replace(tx(NOW + HOUR, amount=40.0), subject_wallet_id="w-nobody"))
    unknown_agent = on_app(tx(NOW + HOUR, "cash_out", amount=40.0, counterparty="A-nobody"))
    high = drain(NOW + 2 * HOUR)

    outcomes = scoring.assess_many([usual, repeated, conflicting, unknown, unknown_agent, high])

    first, replay, conflict, nobody, nowhere, alert = outcomes
    assert isinstance(first, Scored) and not first.replayed
    assert isinstance(replay, Scored) and replay.replayed
    assert replay.assessment == first.assessment
    assert isinstance(conflict, records.TransactionConflict)
    assert isinstance(nobody, records.UnknownParty) and "w-nobody" in str(nobody)
    assert isinstance(nowhere, records.UnknownParty) and "A-nobody" in str(nowhere)
    assert isinstance(alert, Scored) and alert.assessment.risk_band.value == "High"
    assert _rows(world, "SELECT count(*) FROM alerts")[0][0] == 1
    # Nothing was stored for what was rejected; the rest is stored once.
    assert (
        _rows(
            world,
            f"SELECT count(*) FROM transactions WHERE transaction_id IN"
            f" ('{unknown.transaction_id}', '{unknown_agent.transaction_id}')",
        )[0][0]
        == 0
    )
    # Again, all of it is a replay: no new Alert, and the same assessments.
    again = scoring.assess_many([usual, high])
    assert all(isinstance(o, Scored) and o.replayed for o in again)
    assert [o.assessment for o in again if isinstance(o, Scored)] == [
        first.assessment,
        alert.assessment,
    ]
    assert _rows(world, "SELECT count(*) FROM alerts")[0][0] == 1
    # A stored Transaction under another payload is a conflict in a later batch, too.
    (late,) = scoring.assess_many([conflicting])
    assert isinstance(late, records.TransactionConflict)


def test_a_batch_that_fails_stores_nothing(world: str):
    scoring = _scoring(world, None)
    fine = on_app(tx(NOW, amount=40.0))
    # Every row is stored in one transaction, so a database error half way leaves none of them.
    engine = create_engine(world)
    with engine.begin() as connection:
        connection.execute(sa.text(f"REVOKE INSERT ON risk_assessments FROM {APP_ROLE}"))
    engine.dispose()
    with pytest.raises(sa.exc.DBAPIError):
        scoring.assess_many([fine, on_app(tx(NOW + HOUR, amount=41.0))])
    assert (
        _rows(
            world,
            f"SELECT count(*) FROM transactions WHERE transaction_id = '{fine.transaction_id}'",
        )[0][0]
        == 0
    )


def test_the_simulation_clock_does_not_ask_postgres_to_stand_still(database: str):
    engine = create_engine(as_app_role(database))
    statements: list[str] = []
    sa.event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
    clock = SimulationClock(engine)

    assert clock.advance(NOW)
    assert len(statements) == 1
    assert not clock.advance(NOW)
    assert not clock.advance(NOW - DAY)
    assert len(statements) == 1  # answered from what this process knows
    assert clock.advance(NOW + HOUR)
    # Another process moved it further: this one's advance finds it so, and says so.
    other = SimulationClock(engine)
    assert other.advance(NOW + 5 * HOUR)
    assert not clock.advance(NOW + 2 * HOUR)
    assert clock.now() == NOW + 5 * HOUR
    engine.dispose()


def test_a_cold_batch_loads_the_sets_it_touches(world: str, redis_url: str):
    client = redis.Redis.from_url(redis_url.rsplit("/", 1)[0] + "/3")
    client.flushdb()
    cache = RedisHistoryCache(client)
    scoring = _scoring(world, None, cache)
    batch = [on_app(tx(NOW + i * HOUR, amount=40.0 + i)) for i in range(4)]
    batch.append(on_app(tx(NOW + 5 * HOUR, "cash_out", amount=50.0, counterparty=AGENT)))
    batch.append(on_app(tx(NOW + 6 * HOUR, amount=50.0, counterparty=STRANGER)))

    scoring.assess_many(batch)

    engine = create_engine(world)
    with engine.connect() as connection:
        # The subject, receiver and agent sets answer from Redis, not by falling back.
        answered = cache.read_many(
            [
                ("subject", SUBJECT, None, NOW + DAY),
                ("receiver", STRANGER, NOW, NOW + DAY),
                ("agent", AGENT, NOW, NOW + DAY),
            ]
        )
        assert all(a is not None for a in answered)
        subject = PostgresHistory(connection).subject_history(SUBJECT, NOW + DAY).transactions
        assert answered[0] == list(subject)
    engine.dispose()
    client.close()


def test_scoring_one_by_one_still_uses_one_connection_for_a_call(world: str):
    engine = create_engine(as_app_role(world))
    checkouts: list[int] = []
    sa.event.listen(engine, "checkout", lambda *args: checkouts.append(1))
    api = settings(world)
    scoring = StoredScoring(engine, features=api.features(), rules=api.rules())

    scoring.assess(on_app(tx(NOW, amount=40.0)))  # scored and stored
    scoring.assess(on_app(tx(NOW + HOUR, amount=40.0)))
    assert len(checkouts) == 2

    replay = on_app(tx(NOW + 2 * HOUR, amount=40.0))
    scoring.assess(replay)
    checkouts.clear()
    assert scoring.assess(replay).replayed
    assert len(checkouts) == 1
    engine.dispose()
