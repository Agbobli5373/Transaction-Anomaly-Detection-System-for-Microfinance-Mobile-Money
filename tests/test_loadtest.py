"""The load-test tooling: what is sent, how a stream run is read, and the verdict on the targets.

Locust itself and the full-rate runs are not tests: they need a stack of containers and a machine
to themselves (see README, Load tests). What is checked here is everything those runs rely on.
"""

import json
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from confluent_kafka import Consumer, TopicPartition
from conftest import DEFAULT_CONFIG
from market_helpers import Market, build_market

from txrisk.api.schemas import TransactionIn
from txrisk.cli import main
from txrisk.events import Publisher, Topics, ensure_topics
from txrisk.generator import GeneratorConfig, write_dataset
from txrisk.loadtest import (
    ConsumerLag,
    LagSample,
    StreamResult,
    Workload,
    load_loadtest_config,
    load_pool,
    summarise_stream,
)

TARGETS = DEFAULT_CONFIG.parent / "loadtest.toml"
STATS_HEADER = "Type,Name,Request Count,Failure Count,Requests/s,95%"


def stats_csv(path: Path, *, requests: int = 60_000, failures: int = 0, rps: float, p95: float):
    path.write_text(
        f"{STATS_HEADER}\n"
        f"POST,/v1/score,{requests},{failures},{rps},{p95}\n"
        f",Aggregated,{requests},{failures},{rps},{p95}\n",
        encoding="utf-8",
    )
    return path


def stream_json(path: Path, **overrides: float | int | None) -> Path:
    result = StreamResult(
        published=90_000,
        offered_per_second=1500.0,
        consumed_per_second=1200.0,
        peak_lag=20_000,
        recovery_seconds=25.0,
        recovered_below=500,
    )
    path.write_text(json.dumps({**asdict(result), **overrides}), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def market(small_config: GeneratorConfig) -> Market:
    return build_market(small_config)


@pytest.fixture
def dataset(market: Market, tmp_path: Path) -> Path:
    write_dataset(market.dataset, tmp_path / "data")
    return tmp_path / "data"


def test_the_workload_sends_the_datasets_latest_transactions_under_fresh_ids(dataset: Path):
    pool = load_pool(dataset, size=50)
    workload = Workload(pool, run_id="a")

    sent = [workload.next() for _ in range(120)]

    # More draws than the pool holds: every ID is still new, and none is the dataset's own.
    assert len({t.transaction_id for t in sent}) == 120
    assert all(t.transaction_id.startswith("load-a-") for t in sent)
    assert [t.subject_wallet_id for t in sent[:50]] == [t.subject_wallet_id for t in pool]
    assert [t.occurred_at for t in sent[:50]] == sorted(t.occurred_at for t in pool)


def test_a_second_run_uses_ids_the_first_did_not(dataset: Path):
    pool = load_pool(dataset, size=10)

    first = {Workload(pool, run_id="a").next().transaction_id}
    second = {Workload(pool, run_id="b").next().transaction_id}

    assert not first & second


def test_every_transaction_in_the_workload_is_a_valid_sync_api_request(dataset: Path):
    workload = Workload(load_pool(dataset, size=300), run_id="a")

    for _ in range(300):
        body = json.loads(workload.next().model_dump_json(exclude_none=True))
        TransactionIn.model_validate(body)


def test_a_workload_needs_something_to_send():
    with pytest.raises(ValueError, match="no Transactions"):
        Workload([], run_id="a")


def _samples(rows: list[tuple[float, int, int]]) -> list[LagSample]:
    return [LagSample(at=at, committed=c, end=e) for at, c, e in rows]


def test_a_stream_run_reports_throughput_peak_lag_and_how_long_the_lag_took_to_recover():
    # 2,000/s offered for 10 s; the workers manage 1,200/s, then drain the backlog.
    samples = _samples(
        [
            (0, 100, 100),
            (5, 6_100, 10_100),
            (10, 12_100, 20_100),
            (15, 18_100, 20_100),
            (20, 20_100, 20_100),
            (25, 20_100, 20_100),
        ]
    )

    result = summarise_stream(samples, offered_until=10, recovered_below=500)

    assert result.published == 20_000
    assert result.offered_per_second == 2_000
    assert result.consumed_per_second == 1_200
    assert result.peak_lag == 8_000
    assert result.recovery_seconds == 10


def test_a_lag_that_never_recovers_has_no_recovery_time():
    samples = _samples([(0, 0, 0), (10, 5_000, 20_000), (20, 10_000, 20_000)])

    result = summarise_stream(samples, offered_until=10, recovered_below=500)

    assert result.recovery_seconds is None


def test_a_run_whose_lag_never_built_up_recovered_at_once():
    samples = _samples([(0, 0, 0), (10, 10_000, 10_050), (11, 10_050, 10_050)])

    assert summarise_stream(samples, offered_until=10, recovered_below=500).recovery_seconds == 1


def test_a_stream_run_needs_samples_to_read():
    with pytest.raises(ValueError, match="at least two"):
        summarise_stream(_samples([(0, 0, 0)]), offered_until=10, recovered_below=0)


def test_the_committed_targets_are_the_ones_in_the_spec():
    targets = load_loadtest_config(TARGETS)

    assert (targets.sync.requests_per_second, targets.sync.p95_ms) == (200, 200)
    assert targets.stream.transactions_per_second == 1000
    assert targets.stream.lag_recovery_seconds == 60


def test_a_sync_run_at_the_target_rate_under_the_p95_passes(tmp_path: Path, capsys):
    stats = stats_csv(tmp_path / "run_stats.csv", rps=199.2, p95=87)

    assert main(["loadtest", "check", "--sync", str(stats), "--config", str(TARGETS)]) == 0

    assert "FAIL" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("figures", "failed"),
    [
        ({"rps": 200.0, "p95": 200}, "p95"),
        ({"rps": 200.0, "p95": 450}, "p95"),
        ({"rps": 120.0, "p95": 40}, "rate"),
        ({"rps": 200.0, "p95": 40, "failures": 3}, "failures"),
        ({"rps": 200.0, "p95": 40, "requests": 0}, "failures"),
    ],
)
def test_a_sync_run_that_misses_a_target_fails_and_says_which(
    tmp_path: Path, capsys, figures: dict[str, float], failed: str
):
    stats = stats_csv(tmp_path / "run_stats.csv", **figures)  # type: ignore[arg-type]

    assert main(["loadtest", "check", "--sync", str(stats), "--config", str(TARGETS)]) == 1

    out = capsys.readouterr().out
    assert [line.split(":")[0] for line in out.splitlines() if line.startswith("FAIL")] == [
        f"FAIL  sync {failed}"
    ]


def test_a_stream_run_that_keeps_up_and_recovers_passes(tmp_path: Path, capsys):
    stream = stream_json(tmp_path / "stream.json")

    assert main(["loadtest", "check", "--stream", str(stream), "--config", str(TARGETS)]) == 0

    assert "FAIL" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("overrides", "failed"),
    [
        ({"consumed_per_second": 700.0}, "throughput"),
        ({"offered_per_second": 600.0, "consumed_per_second": 600.0}, "offered"),
        ({"recovery_seconds": 75.0}, "lag recovery"),
        ({"recovery_seconds": None}, "lag recovery"),
    ],
)
def test_a_stream_run_that_misses_a_target_fails_and_says_which(
    tmp_path: Path, capsys, overrides: dict[str, float | None], failed: str
):
    stream = stream_json(tmp_path / "stream.json", **overrides)

    assert main(["loadtest", "check", "--stream", str(stream), "--config", str(TARGETS)]) == 1

    fails = [line for line in capsys.readouterr().out.splitlines() if line.startswith("FAIL")]
    assert any(f"stream {failed}:" in line for line in fails)


def test_a_failing_run_fails_the_check_even_if_the_other_passes(tmp_path: Path):
    good = stats_csv(tmp_path / "good_stats.csv", rps=200.0, p95=60)
    bad = stream_json(tmp_path / "stream.json", consumed_per_second=10.0)

    exit_code = main(
        ["loadtest", "check", "--sync", str(good), "--stream", str(bad), "--config", str(TARGETS)]
    )

    assert exit_code == 1


def test_the_check_needs_a_run_to_judge(capsys):
    assert main(["loadtest", "check", "--config", str(TARGETS)]) == 2

    assert "--sync" in capsys.readouterr().err


def test_the_check_refuses_a_file_that_is_not_a_locust_stats_csv(tmp_path: Path, capsys):
    other = tmp_path / "other.csv"
    other.write_text("a,b\n1,2\n", encoding="utf-8")

    assert main(["loadtest", "check", "--sync", str(other), "--config", str(TARGETS)]) == 2

    assert "Aggregated" in capsys.readouterr().err


def test_consumer_lag_is_what_the_topic_holds_beyond_the_groups_commits(redpanda: str):
    topics = Topics(f"lag{time.monotonic_ns()}.")
    ensure_topics(redpanda, topics, partitions=3)
    publisher = Publisher(redpanda)
    for n in range(30):
        publisher.publish_raw(topics.transactions, f"wallet-{n}".encode(), b"{}")
    publisher.flush()
    lag = ConsumerLag(redpanda, topics.transactions, "lag-test")

    before = lag.read(at=0.0)
    # A worker that has consumed and committed what the first partition holds so far.
    consumer = Consumer(
        {"bootstrap.servers": redpanda, "group.id": "lag-test", "enable.auto.commit": False}
    )
    low, high = consumer.get_watermark_offsets(TopicPartition(topics.transactions, 0), timeout=10)
    consumer.commit(offsets=[TopicPartition(topics.transactions, 0, high)], asynchronous=False)
    consumer.close()
    after = lag.read(at=1.0)
    lag.close()

    assert (before.committed, before.end, before.lag) == (0, 30, 30)
    assert after.end == 30
    assert after.lag == 30 - high
    assert high > low
