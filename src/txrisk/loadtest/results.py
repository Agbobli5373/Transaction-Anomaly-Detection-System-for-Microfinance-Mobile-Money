"""Judging load-test results against the targets in `config/loadtest.toml`.

A sync run is Locust's `--csv` output: its `_stats.csv` holds the aggregated requests, with the
p95 and the requests a second. A stream run is the JSON a `loadtests/stream_locustfile.py` run
writes (a `StreamResult`). `check_sync` and `check_stream` return one `Check` for each target, so a
report says which held and which did not, with the numbers.
"""

import csv
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from txrisk.loadtest.lag import StreamResult

_AGGREGATED = "Aggregated"


class SyncTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests_per_second: float = Field(gt=0)
    p95_ms: float = Field(gt=0)
    rate_tolerance: float = Field(gt=0, le=1)


class StreamTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transactions_per_second: float = Field(gt=0)
    lag_recovered_events: int = Field(ge=0)
    lag_recovery_seconds: float = Field(gt=0)


class LoadTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sync: SyncTargets
    stream: StreamTargets


def load_loadtest_config(path: Path) -> LoadTestConfig:
    with path.open("rb") as f:
        return LoadTestConfig.model_validate(tomllib.load(f))


@dataclass(frozen=True)
class Check:
    """One target, and whether the run met it."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SyncResult:
    requests: int
    failures: int
    requests_per_second: float
    p95_ms: float


def read_sync_result(stats_csv: Path) -> SyncResult:
    """The aggregated row of a Locust `<prefix>_stats.csv`."""
    with stats_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("Name") == _AGGREGATED:
                return SyncResult(
                    requests=int(row["Request Count"]),
                    failures=int(row["Failure Count"]),
                    requests_per_second=float(row["Requests/s"]),
                    p95_ms=float(row["95%"]),
                )
    raise ValueError(f"{stats_csv} has no {_AGGREGATED} row: is it a Locust _stats.csv?")


def check_sync(result: SyncResult, targets: SyncTargets) -> list[Check]:
    """The sync API at the target rate: it was reached, nothing failed, and the p95 held."""
    floor = targets.requests_per_second * targets.rate_tolerance
    return [
        Check(
            "rate",
            result.requests_per_second >= floor,
            f"{result.requests_per_second:.1f} requests/s served; at least {floor:.1f} "
            f"(target {targets.requests_per_second:g}) needed",
        ),
        Check(
            "failures",
            result.failures == 0 and result.requests > 0,
            f"{result.failures} of {result.requests} requests failed",
        ),
        Check(
            "p95",
            result.p95_ms < targets.p95_ms,
            f"p95 {result.p95_ms:.0f} ms; under {targets.p95_ms:g} ms needed",
        ),
    ]


def read_stream_result(path: Path) -> StreamResult:
    return StreamResult(**json.loads(path.read_text(encoding="utf-8")))


def write_stream_result(result: StreamResult, path: Path) -> None:
    path.write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")


def check_stream(result: StreamResult, targets: StreamTargets) -> list[Check]:
    """The stream at the target rate: the producer offered it, the workers consumed it, and the
    lag was back to normal in time after the burst."""
    wanted = targets.transactions_per_second
    recovery = result.recovery_seconds
    return [
        Check(
            "offered",
            result.offered_per_second >= wanted,
            f"{result.offered_per_second:.0f} Transactions/s published; at least {wanted:g} "
            "needed for the run to count",
        ),
        Check(
            "throughput",
            result.consumed_per_second >= wanted,
            f"{result.consumed_per_second:.0f} Transactions/s consumed "
            f"(peak lag {result.peak_lag:,}); at least {wanted:g} needed",
        ),
        Check(
            "lag recovery",
            recovery is not None and recovery <= targets.lag_recovery_seconds,
            f"lag never fell to {result.recovered_below:,} events"
            if recovery is None
            else f"lag back under {result.recovered_below:,} events {recovery:.0f} s after the "
            f"burst; within {targets.lag_recovery_seconds:g} s needed",
        ),
    ]
