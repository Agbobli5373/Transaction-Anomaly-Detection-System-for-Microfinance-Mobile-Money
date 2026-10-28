"""The Redis recent-history cache against real Redis and Postgres: it holds 30 days of event time,
answers exactly as Postgres does (so as the offline replay does), and never changes a score."""

import dataclasses
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
import redis
import sqlalchemy as sa
from api_helpers import settings
from conftest import create_database
from redis.backoff import NoBackoff
from redis.retry import Retry
from scoring_helpers import DAY, HOUR, NOW, REGISTERED, SUBJECT, tx

from txrisk.api import ApiSettings
from txrisk.features import (
    FeatureConfig,
    Transaction,
    transaction_features,
    transactions_from_frame,
)
from txrisk.features.table import replay_history
from txrisk.generator import GeneratorConfig, generate, write_dataset
from txrisk.store import (
    PostgresHistory,
    RedisHistoryCache,
    StoredScoring,
    create_engine,
    records,
    seed,
)

FRIEND = "w-friend"
AGENT = "A00001"


@pytest.fixture
def cache(redis_url: str) -> Iterator[RedisHistoryCache]:
    """A cache over the shared Redis, emptied first."""
    client = redis.Redis.from_url(redis_url)
    client.flushall()
    yield RedisHistoryCache(client)
    client.close()


@pytest.fixture
def engine(database: str) -> Iterator[sa.Engine]:
    engine = create_engine(database)
    with engine.begin() as connection:
        for wallet in (SUBJECT, FRIEND):
            records.add_wallet(
                connection,
                wallet,
                kyc_tier="minimum",
                home_region="Ashanti",
                registered_at=REGISTERED,
            )
        records.add_agent(connection, AGENT, region="Ashanti")
    yield engine
    engine.dispose()


def store(engine: sa.Engine, cache: RedisHistoryCache | None, *transactions: Transaction) -> None:
    """Store Transactions in Postgres and then, as the worker does, in the cache."""
    for t in transactions:
        with engine.begin() as connection:
            records.record_transaction(connection, t)
        if cache is not None:
            with engine.connect() as connection:
                cache.record(connection, t)


def on_app(transaction: Transaction) -> Transaction:
    return dataclasses.replace(transaction, channel="app")


def daily(days: int, *, until: datetime = NOW) -> list[Transaction]:
    return [on_app(tx(until - DAY * d)) for d in range(days, 0, -1)]


def cash_out(at: datetime, amount: float = 100.0) -> Transaction:
    return on_app(tx(at, "cash_out", amount=amount, counterparty=AGENT))


def assert_same_history(engine: sa.Engine, cache: RedisHistoryCache, before: datetime) -> None:
    """Every history call answers the same from the cache as from Postgres."""
    with engine.connect() as connection:
        cached, postgres = cache.history(connection), PostgresHistory(connection)
        for wallet in (SUBJECT, FRIEND):
            assert cached.subject_history(wallet, before) == postgres.subject_history(
                wallet, before
            )
            for since in (before - HOUR, before - DAY, before - 29 * DAY, before - 45 * DAY):
                assert list(cached.incoming_transfers(wallet, since, before)) == list(
                    postgres.incoming_transfers(wallet, since, before)
                )
        for since in (before - HOUR, before - DAY, before - 29 * DAY, before - 45 * DAY):
            assert list(cached.agent_transactions(AGENT, since, before)) == list(
                postgres.agent_transactions(AGENT, since, before)
            )


def members(cache: RedisHistoryCache, kind: str, party: str) -> int:
    return int(cache._redis.zcard(f"txrisk:history:{kind}:{party}"))  # type: ignore[no-untyped-call]


def test_entries_older_than_30_days_of_event_time_are_trimmed(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(engine, cache, *daily(40))

    # 30 days back from the newest, inclusive: the newest is NOW - 1 day.
    assert members(cache, "subject", SUBJECT) == 31


def test_trimming_follows_event_time_never_the_wall_clock(
    engine: sa.Engine, cache: RedisHistoryCache
):
    # Years in the past: a wall-clock TTL would have dropped every entry already.
    long_ago = datetime(2001, 1, 1, tzinfo=NOW.tzinfo)
    store(engine, cache, *daily(10, until=long_ago))

    assert members(cache, "subject", SUBJECT) == 10


def test_a_wallet_whose_whole_history_is_in_the_window_is_served_from_redis(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(engine, cache, *daily(20))

    with engine.connect() as connection:
        found = cache.history(connection).subject_history(SUBJECT, NOW)
        assert found == PostgresHistory(connection).subject_history(SUBJECT, NOW)
    assert cache.reads == {"hit": 1}
    assert_same_history(engine, cache, NOW)


def test_a_wallet_with_a_partial_window_falls_back_instead_of_returning_partial_history(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(engine, cache, *daily(40))
    assert members(cache, "subject", SUBJECT) < 40

    with engine.connect() as connection:
        found = cache.history(connection).subject_history(SUBJECT, NOW).transactions
    assert len(found) == 40
    assert cache.reads["fallback"] == 1
    assert_same_history(engine, cache, NOW)


def test_windows_inside_the_cached_span_hit_even_for_a_trimmed_set(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(
        engine,
        cache,
        *daily(40),
        cash_out(NOW - 40 * DAY),
        *(cash_out(NOW - HOUR * h) for h in (30, 5, 2)),
    )

    with engine.connect() as connection:
        cached = cache.history(connection)
        found = cached.agent_transactions(AGENT, NOW - DAY, NOW)
        assert cache.reads["hit"] == 1 and len(found) == 2
        # Older than the cache holds: Postgres answers, whole.
        cached.agent_transactions(AGENT, NOW - 60 * DAY, NOW)
    assert cache.reads["fallback"] == 1
    assert_same_history(engine, cache, NOW)


def test_a_set_never_filled_reads_postgres(engine: sa.Engine, cache: RedisHistoryCache):
    store(engine, None, *daily(5))  # stored, but never cached

    assert_same_history(engine, cache, NOW)
    assert cache.reads["hit"] == 0


def test_adding_a_transaction_again_changes_nothing(engine: sa.Engine, cache: RedisHistoryCache):
    transactions = [*daily(5), cash_out(NOW - HOUR)]
    store(engine, cache, *transactions)
    store(engine, cache, *transactions)  # redelivery

    assert members(cache, "subject", SUBJECT) == 6
    assert members(cache, "agent", AGENT) == 1
    assert_same_history(engine, cache, NOW)


def test_transactions_arriving_out_of_event_time_order_are_read_in_order(
    engine: sa.Engine, cache: RedisHistoryCache
):
    transactions = daily(6)
    store(engine, cache, *[transactions[i] for i in (0, 3, 1, 5, 2, 4)])

    with engine.connect() as connection:
        found = cache.history(connection).subject_history(SUBJECT, NOW).transactions
    assert [t.transaction_id for t in found] == [t.transaction_id for t in transactions]
    assert_same_history(engine, cache, NOW)


def test_reads_only_see_transactions_strictly_before_the_clock(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(engine, cache, *daily(5), on_app(tx(NOW)), on_app(tx(NOW + HOUR)))

    with engine.connect() as connection:
        found = cache.history(connection).subject_history(SUBJECT, NOW).transactions
    assert len(found) == 5
    assert_same_history(engine, cache, NOW)


def test_a_cache_filled_from_postgres_after_a_flush_answers_the_same(
    engine: sa.Engine, cache: RedisHistoryCache
):
    store(engine, cache, *daily(15))
    cache.flush()
    store(engine, cache, cash_out(NOW - HOUR))  # refills the sets this one touches

    assert_same_history(engine, cache, NOW)
    assert cache.reads["hit"] >= 1


def test_redis_being_unreachable_falls_back_to_postgres_on_read_and_fails_the_write(
    engine: sa.Engine,
):
    store(engine, None, *daily(5))
    unreachable = RedisHistoryCache(
        redis.Redis(port=1, socket_connect_timeout=0.2, retry=Retry(NoBackoff(), 0))
    )

    assert_same_history(engine, unreachable, NOW)
    assert unreachable.reads["hit"] == 0
    with pytest.raises(redis.RedisError), engine.connect() as connection:
        unreachable.record(connection, on_app(tx(NOW)))


def test_without_a_redis_url_there_is_no_cache(database: str):
    assert settings(database).history_cache() is None
    configured = ApiSettings(database_url=database, redis_url="redis://localhost:6379/0")
    assert isinstance(configured.history_cache(), RedisHistoryCache)


# A market long enough for Wallets and Agents to outlive the 30-day window.
DAYS = 40


@pytest.fixture(scope="module")
def long_market(default_config: GeneratorConfig, tmp_path_factory: pytest.TempPathFactory):
    config = default_config.model_copy(
        update={
            "period": default_config.period.model_copy(update={"days": DAYS}),
            "population": default_config.population.model_copy(
                update={"wallets": 40, "agents": 3, "merchants": 4}
            ),
        }
    )
    dataset = generate(config, seed=9, key=b"test-pseudonym-key")
    folder = tmp_path_factory.mktemp("long-market")
    write_dataset(dataset, folder)
    feature_config = FeatureConfig(
        kyc_limits={tier.name: tier.single_transaction_limit for tier in config.market.kyc_tiers}
    )
    return dataset, folder, feature_config


@pytest.fixture(scope="module")
def parties(postgres: str, migrated_template: str, long_market) -> str:
    """A template database with every party and SIM swap stored, and no Transactions."""
    dataset, folder, _ = long_market
    url = create_database(postgres, "long_market_parties", template=migrated_template)
    engine = create_engine(url)
    seed(engine, folder, until=dataset.transactions["occurred_at"].min().to_pydatetime())
    with engine.begin() as connection:
        for wallet_id, occurred_at in zip(
            dataset.sim_swaps["wallet_id"], dataset.sim_swaps["occurred_at"], strict=True
        ):
            records.add_sim_swap(connection, wallet_id, occurred_at.floor("us").to_pydatetime())
    engine.dispose()  # a template cannot be copied while anyone is connected to it
    return "long_market_parties"


_copies = iter(range(1000))


def fresh_database(postgres: str, template: str) -> str:
    return create_database(postgres, f"copy_{next(_copies)}", template=template)


def scoring(url: str, cache: RedisHistoryCache | None, feature_config: FeatureConfig):
    api = settings(url)
    return StoredScoring(
        create_engine(url), features=feature_config, rules=api.rules(), cache=cache
    )


def chronological(dataset) -> list[Transaction]:
    return transactions_from_frame(dataset.transactions.sort_values("occurred_at", kind="stable"))


def stored_scores(url: str) -> list[tuple[object, ...]]:
    engine = create_engine(url)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                "SELECT transaction_id, risk_score, risk_band, reasons::text,"
                " component_scores::text FROM risk_assessments ORDER BY transaction_id"
            )
        ).all()
    engine.dispose()
    return [tuple(row) for row in rows]


def test_features_from_the_redis_provider_equal_the_offline_replay_features(
    postgres: str,
    parties: str,
    long_market,
    cache: RedisHistoryCache,
    tmp_path: Path,
):
    dataset, _, feature_config = long_market
    url = fresh_database(postgres, parties)
    scorer = scoring(url, cache, feature_config)
    replay = replay_history(dataset.wallets, dataset.sim_swaps)
    engine = create_engine(url)
    compared = 0
    for i, transaction in enumerate(chronological(dataset)):
        if i % 3 == 0:
            with engine.connect() as connection:
                online = transaction_features(
                    transaction,
                    cache.history(connection),
                    now=transaction.occurred_at,
                    config=feature_config,
                )
            offline = transaction_features(
                transaction, replay, now=transaction.occurred_at, config=feature_config
            )
            assert online == offline, transaction
            compared += 1
        scorer.assess(transaction)
        replay.record(transaction)
    engine.dispose()

    assert compared > 100
    # Both paths were exercised: Wallets and Agents past 30 days read Postgres.
    assert cache.reads["hit"] > 0 and cache.reads["fallback"] > 0


def test_flushing_redis_between_batches_changes_no_scores(
    postgres: str, parties: str, long_market, cache: RedisHistoryCache
):
    dataset, _, feature_config = long_market
    transactions = chronological(dataset)
    plain = fresh_database(postgres, parties)
    flushed = fresh_database(postgres, parties)
    without_cache = scoring(plain, None, feature_config)
    with_cache = scoring(flushed, cache, feature_config)

    batch = 150
    for start in range(0, len(transactions), batch):
        for transaction in transactions[start : start + batch]:
            without_cache.assess(transaction)
            with_cache.assess(transaction)
        cache.flush()

    scores = stored_scores(plain)
    assert len(scores) == len(transactions)
    assert stored_scores(flushed) == scores
    assert cache.reads["hit"] > 0


def test_a_replayed_transaction_repairs_a_cache_that_missed_it(
    postgres: str, parties: str, long_market, cache: RedisHistoryCache
):
    dataset, _, feature_config = long_market
    transactions = chronological(dataset)[:200]
    url = fresh_database(postgres, parties)
    uncached = scoring(url, None, feature_config)
    for transaction in transactions:
        uncached.assess(transaction)  # stored without the cache, as if its add had failed

    cached = scoring(url, cache, feature_config)
    for transaction in transactions:
        assert cached.assess(transaction).replayed

    # Every set now holds what Postgres does.
    engine = create_engine(url)
    last = dataclasses.replace(transactions[-1], occurred_at=transactions[-1].occurred_at + HOUR)
    with engine.connect() as connection:
        for wallet in {t.subject_wallet_id for t in transactions}:
            assert cache.history(connection).subject_history(
                wallet, last.occurred_at
            ) == PostgresHistory(connection).subject_history(wallet, last.occurred_at)
    engine.dispose()
    assert cache.reads["fallback"] == 0
