"""Locust load test of the stream path: Transactions published to `transaction.created` at a set
rate, while the scoring workers consume them.

    LOADTEST_DATA=data/generated LOADTEST_RATE=1500 \\
      uv run locust -f loadtests/stream_locustfile.py --headless --users 4 --spawn-rate 4 \\
      --run-time 60s --host stream

`--host` is not used (Locust wants one). Start the workers first (`docker compose up --scale
worker=3`), after `make seed`. The users together publish `LOADTEST_RATE` Transactions a second,
in batches, each under a fresh ID. A sampler reads the workers' consumer group offsets every
second, and when the run time is up it waits for the lag to recover (at most
`LOADTEST_RECOVERY_WINDOW` seconds) and writes the run's `StreamResult` as JSON to
`LOADTEST_OUT` (reports/loadtest/stream.json); `txrisk loadtest check --stream` judges it.

A burst is the same run at a rate above what the workers can score: lag builds while it lasts, and
the recovery time is how long the workers then take to catch up.
"""

import os
import time
import uuid
from pathlib import Path
from typing import Any

import gevent
from locust import User, events, task

from txrisk.events import Publisher, Topics, encode, ensure_topics
from txrisk.loadtest import (
    ConsumerLag,
    LagSample,
    Workload,
    load_loadtest_config,
    load_pool,
    summarise_stream,
    write_stream_result,
)

RATE = float(os.environ.get("LOADTEST_RATE", "1500"))
BATCH = int(os.environ.get("LOADTEST_BATCH", "100"))
BOOTSTRAP = os.environ.get("TXRISK_KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
GROUP = os.environ.get("LOADTEST_GROUP", os.environ.get("TXRISK_CONSUMER_GROUP", "txrisk-scoring"))
PARTITIONS = int(os.environ.get("TXRISK_TOPIC_PARTITIONS", "3"))
RECOVERY_WINDOW = float(os.environ.get("LOADTEST_RECOVERY_WINDOW", "180"))
OUT = Path(os.environ.get("LOADTEST_OUT", "reports/loadtest/stream.json"))
TARGETS = Path(os.environ.get("LOADTEST_CONFIG", "config/loadtest.toml"))

TOPICS = Topics(os.environ.get("TXRISK_TOPIC_PREFIX", ""))
WORKLOAD = Workload(
    load_pool(
        Path(os.environ.get("LOADTEST_DATA", "data/generated")),
        size=int(os.environ.get("LOADTEST_POOL", "20000")),
    ),
    run_id=uuid.uuid4().hex[:8],
)
PUBLISHER = Publisher(BOOTSTRAP)


class Ledger(User):
    """Stands in for the ledger, publishing a batch of Transactions at its share of the rate."""

    def on_start(self) -> None:
        self._next = time.monotonic()

    def wait_time(self) -> float:
        users = max(self.environment.runner.user_count, 1) if self.environment.runner else 1
        self._next += users * BATCH / RATE
        return max(self._next - time.monotonic(), 0.0)

    @task
    def publish_batch(self) -> None:
        started = time.perf_counter()
        for _ in range(BATCH):
            event = WORKLOAD.next()
            PUBLISHER.publish_raw(
                TOPICS.transactions, event.subject_wallet_id.encode(), encode(event)
            )
        self.environment.events.request.fire(
            request_type="PUBLISH",
            name=TOPICS.transactions,
            response_time=(time.perf_counter() - started) * 1000,
            response_length=BATCH,
            exception=None,
            context={},
        )


class _Run:
    """The lag samples of the run in progress, and the greenlet taking them."""

    def __init__(self) -> None:
        self.lag = ConsumerLag(BOOTSTRAP, TOPICS.transactions, GROUP)
        self.started = time.monotonic()
        self.samples: list[LagSample] = [self.lag.read(at=0.0)]
        self.sampler = gevent.spawn(self._sample)

    def _sample(self) -> None:
        while True:
            gevent.sleep(1.0)
            self.samples.append(self.lag.read(at=time.monotonic() - self.started))

    def elapsed(self) -> float:
        return time.monotonic() - self.started


_run: _Run | None = None


@events.test_start.add_listener
def _start(environment: Any, **_: Any) -> None:
    global _run
    ensure_topics(BOOTSTRAP, TOPICS, partitions=PARTITIONS)
    _run = _Run()


@events.test_stop.add_listener
def _stop(environment: Any, **_: Any) -> None:
    """Publishing has stopped: wait for the workers to drain the lag, then write the result."""
    run = _run
    if run is None:
        return
    offered_until = run.elapsed()
    PUBLISHER.flush()
    recovered_below = load_loadtest_config(TARGETS).stream.lag_recovered_events
    while run.elapsed() - offered_until < RECOVERY_WINDOW:
        gevent.sleep(1.0)
        if run.samples[-1].at > offered_until and run.samples[-1].lag <= recovered_below:
            break
    run.sampler.kill()
    run.lag.close()
    result = summarise_stream(
        run.samples, offered_until=offered_until, recovered_below=recovered_below
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    write_stream_result(result, OUT)
    print(f"stream result written to {OUT}: {result}")
