"""Locust load test of the sync API: `POST /v1/score` at a constant rate.

    LOADTEST_RPS=200 uv run locust -f loadtests/locustfile.py --headless \\
      --host http://localhost:8000 --users 40 --spawn-rate 40 --run-time 120s --reset-stats \\
      --csv reports/loadtest/sync

The users share the offered rate (`LOADTEST_RPS`) equally, whatever their number, and each request
carries a Transaction of the dataset under a fresh ID. The caller is a service's API key with the
`score:write` scope and a rate limit above the offered rate, read from the file
`reports/loadtest/api-key` (`LOADTEST_KEY_FILE`), which git ignores. A response that is not 200,
or whose `degraded` flag is not what the run expects, counts as a failure: a run against a
healthy stack must not quietly be scored by the rules alone, and a Degraded mode run
(`LOADTEST_EXPECT_DEGRADED=1`) must be.
"""

import os
import time
import uuid
from pathlib import Path

from locust import HttpUser, task

from txrisk.api.schemas import TransactionIn
from txrisk.loadtest import Workload, load_pool

CREDENTIAL = (
    Path(os.environ.get("LOADTEST_KEY_FILE", "reports/loadtest/api-key")).read_text().strip()
)
RATE = float(os.environ.get("LOADTEST_RPS", "200"))
EXPECT_DEGRADED = os.environ.get("LOADTEST_EXPECT_DEGRADED", "") == "1"
POOL_SIZE = int(os.environ.get("LOADTEST_POOL", "20000"))

WORKLOAD = Workload(
    load_pool(Path(os.environ.get("LOADTEST_DATA", "data/generated")), size=POOL_SIZE),
    run_id=uuid.uuid4().hex[:8],
)


class Caller(HttpUser):
    """A ledger-side service scoring each Transaction as it happens."""

    def on_start(self) -> None:
        self.client.headers["X-API-Key"] = CREDENTIAL
        self._next = time.monotonic()

    def wait_time(self) -> float:
        # Each user's share of the rate; the deadline moves on by a fixed interval, so a slow
        # response is made up for and the offered rate holds.
        users = max(self.environment.runner.user_count, 1) if self.environment.runner else 1
        self._next += users / RATE
        return max(self._next - time.monotonic(), 0.0)

    @task
    def score(self) -> None:
        body = WORKLOAD.next().model_dump(mode="json", exclude_none=True)
        TransactionIn.model_validate(body)  # a bad workload is a bug here, not an API failure
        with self.client.post("/v1/score", json=body, catch_response=True) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            degraded = bool(response.json()["degraded"])
            if degraded != EXPECT_DEGRADED:
                response.failure(f"degraded={degraded}, expected {EXPECT_DEGRADED}")
