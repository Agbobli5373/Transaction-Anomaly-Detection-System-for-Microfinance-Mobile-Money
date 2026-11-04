"""Where the time of one scoring goes: per-stage timings of `StoredScoring.assess`.

    TXRISK_DATABASE_URL=... TXRISK_REDIS_URL=... TXRISK_MODEL_BUNDLE=models/<version> \\
      uv run python loadtests/profile_scoring.py --count 300

Run it against a seeded Postgres (`txrisk seed`, README "Load tests"). It scores `--count`
Transactions of the dataset's last 20,000, each under a fresh ID, one after another through
`StoredScoring`, as the sync API does, and prints the median and mean of each stage in
milliseconds plus the Postgres statements one scoring issues. `--no-model` scores with the rules
alone (what the stream workers run in the load tests). `--batch N` scores them through the
batch path in groups of N instead, as a stream worker does, and prints the time per Transaction.
`--profile out.prof` writes a cProfile of the timed run.

The stages are measured by wrapping the functions the scoring path calls, so they add up to a
little less than the total: what is left is called "other" (the Python between the stages).
"""

import argparse
import cProfile
import statistics
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from txrisk.api.settings import ApiSettings
from txrisk.loadtest import load_pool
from txrisk.store import StoredScoring, create_app_engine

# (module path, attribute) of what to time, and the stage it is counted in. A function that is
# gone (the scoring path was reorganised) is skipped, so the same script profiles both.
_STAGES: list[tuple[str, str, str]] = [
    ("txrisk.scoring.service", "transaction_features", "features"),
    ("txrisk.scoring.service", "rule_hits", "rules"),
    ("txrisk.scoring.service", "inputs_for", "model inputs"),
    ("txrisk.scoring.service", "input_matrix", "model inputs"),
    ("txrisk.models.bundle", "ModelBundle.score_rows", "models"),
    ("txrisk.models.bundle", "ModelBundle.contribution_rows", "explanations"),
    ("txrisk.models.bundle", "ModelBundle.components", "models"),
    ("txrisk.models.bundle", "ModelBundle.risk_scores", "models"),
    ("txrisk.models.bundle", "ModelBundle.contributions", "explanations"),
    ("txrisk.scoring.service", "stacked_reasons", "explanations"),
]


class _Timings:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = defaultdict(list)
        self._open: dict[str, float] = defaultdict(float)
        self.statements = 0
        self._db = 0.0
        self._by_statement: dict[str, float] = defaultdict(float)

    def add(self, stage: str, seconds: float) -> None:
        self._open[stage] += seconds

    def statement(self, text: str, seconds: float) -> None:
        self.statements += 1
        self._db += seconds
        words = text.split()
        label = (
            " ".join(words[:2])
            + " "
            + (
                words[words.index("FROM") + 1]
                if "FROM" in words
                else words[2]
                if len(words) > 2
                else ""
            )
        )
        self._by_statement[label] += seconds

    def finish(self, total: float) -> None:
        """Close one scoring: its stage totals become one sample each."""
        stages = dict(self._open)
        stages["postgres statements"] = self._db
        for label, seconds in self._by_statement.items():
            stages[f"  sql {label}"] = seconds
        stages["other"] = total - sum(
            v for k, v in stages.items() if k != "postgres statements" and not k.startswith("  sql")
        )
        for stage, seconds in stages.items():
            self.samples[stage].append(seconds)
        self.samples["statements per scoring"].append(float(self.statements))
        self.samples["total"].append(total)
        self._open.clear()
        self._by_statement.clear()
        self.statements = 0
        self._db = 0.0


def _wrap(timings: _Timings, stage: str, function: Callable[..., Any]) -> Callable[..., Any]:
    def timed(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            timings.add(stage, time.perf_counter() - start)

    return timed


def _install(timings: _Timings) -> None:
    import importlib

    for module_name, attribute, stage in _STAGES:
        module = importlib.import_module(module_name)
        owner: Any = module
        *path, name = attribute.split(".")
        for part in path:
            owner = getattr(owner, part, None)
        if owner is None or not hasattr(owner, name):
            continue
        setattr(owner, name, _wrap(timings, stage, getattr(owner, name)))


def _count_statements(engine: sa.Engine, timings: _Timings) -> None:
    started: list[float] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _before(*_: Any) -> None:
        started.append(time.perf_counter())

    @sa.event.listens_for(engine, "after_cursor_execute")
    def _after(connection: Any, cursor: Any, statement: str, *_: Any) -> None:
        timings.statement(statement, time.perf_counter() - started.pop())


def _print_statements(engine: sa.Engine) -> None:
    @sa.event.listens_for(engine, "before_cursor_execute")
    def _show(connection: Any, cursor: Any, statement: str, *_: Any) -> None:
        print(" ".join(statement.split())[:150])

    @sa.event.listens_for(engine, "commit")
    def _commit(_: Any) -> None:
        print("COMMIT")


def _report(title: str, samples: dict[str, list[float]], *, milliseconds: bool = True) -> None:
    print(f"\n{title}")
    print(f"{'stage':28} {'median':>10} {'mean':>10} {'p95':>10}")
    order = [
        "total",
        "features",
        "rules",
        "model inputs",
        "models",
        "explanations",
        "postgres statements",
        "other",
        "statements per scoring",
    ]
    for stage in [s for s in order if s in samples] + [s for s in samples if s not in order]:
        values = samples[stage]
        scale = 1.0 if stage == "statements per scoring" or not milliseconds else 1000.0
        ordered = sorted(values)
        p95 = ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]
        print(
            f"{stage:28} {statistics.median(values) * scale:10.2f} "
            f"{statistics.fmean(values) * scale:10.2f} {p95 * scale:10.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--count", type=int, default=300, help="Transactions to time")
    parser.add_argument("--warmup", type=int, default=30, help="Transactions scored first")
    parser.add_argument("--data", type=Path, default=Path("data/generated"))
    parser.add_argument("--no-model", action="store_true", help="score with the rules alone")
    parser.add_argument("--batch", type=int, default=0, help="score through the batch path")
    parser.add_argument("--profile", type=Path, help="write a cProfile of the timed run here")
    parser.add_argument(
        "--show-statements", action="store_true", help="print the SQL of one scoring, in order"
    )
    args = parser.parse_args()

    settings = ApiSettings.from_env()
    engine = create_app_engine(settings.database_url)
    scoring = StoredScoring(
        engine,
        features=settings.features(),
        rules=settings.rules(),
        bundle=None if args.no_model else settings.champion(None, engine),
        cache=settings.history_cache(),
    )
    health = scoring.model_health()
    if health.degraded:
        raise SystemExit(f"the model is degraded: {health.reason}")

    run_id = uuid.uuid4().hex[:8]
    pool = load_pool(args.data, size=20_000)
    needed = args.warmup + args.count
    transactions = [
        e.model_copy(update={"transaction_id": f"prof-{run_id}-{n}"}).to_transaction()
        for n, e in enumerate(pool[:needed])
    ]
    warmup, timed = transactions[: args.warmup], transactions[args.warmup :]

    timings = _Timings()
    if args.batch:
        assess_many = getattr(scoring, "assess_many", None)
        if assess_many is None:
            raise SystemExit("this build has no batch path")
        for i in range(0, len(warmup), args.batch):
            assess_many(warmup[i : i + args.batch])
        per_transaction = []
        profiler = cProfile.Profile() if args.profile else None
        if profiler:
            profiler.enable()
        start = time.perf_counter()
        for i in range(0, len(timed), args.batch):
            group = timed[i : i + args.batch]
            began = time.perf_counter()
            assess_many(group)
            per_transaction.extend([(time.perf_counter() - began) / len(group)] * len(group))
        elapsed = time.perf_counter() - start
        if profiler:
            profiler.disable()
            profiler.dump_stats(str(args.profile))
        _report(f"batches of {args.batch}: time per Transaction", {"total": per_transaction})
        print(f"\n{len(timed) / elapsed:.1f} Transactions/s")
        return

    for t in warmup:
        scoring.assess(t)
    _install(timings)
    _count_statements(engine, timings)
    if args.show_statements:
        _print_statements(engine)
        scoring.assess(timed[0])
        raise SystemExit(0)
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    begin = time.perf_counter()
    for t in timed:
        start = time.perf_counter()
        scoring.assess(t)
        timings.finish(time.perf_counter() - start)
    elapsed = time.perf_counter() - begin
    if profiler:
        profiler.disable()
        profiler.dump_stats(str(args.profile))
    label = "rules only" if args.no_model else "Champion model"
    _report(f"one Transaction at a time, {label}, {len(timed)} scored (ms)", timings.samples)
    print(f"\n{len(timed) / elapsed:.1f} Transactions/s on one thread")


if __name__ == "__main__":
    main()
