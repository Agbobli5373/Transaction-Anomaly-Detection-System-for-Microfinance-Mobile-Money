"""The Champion model as the scoring path sees it: loaded lazily, timed, and allowed to be down.

A bundle that is missing or corrupt, or that fails or is too slow when it scores, puts scoring in
Degraded mode (rules only). Rather than reloading or re-trying the model on every Transaction,
`Champion` is a circuit breaker: after a failure it is skipped for `retry_after` seconds, then the
next Transaction (or readiness check) tries it again, and one success ends Degraded mode.

The breaker's pause is operational plumbing, so it reads a monotonic clock, not the simulation
clock: it never decides what a Transaction scores, only whether the model is tried yet.

A call that times out is abandoned, not killed (Python cannot stop a thread): it finishes on its
pool thread and its result is dropped. The pool is bounded, so a model that hangs for good ties up
at most `MAX_SLOW_CALLS` threads; once they are all busy every call times out at once (the
breaker is open by then anyway) instead of piling up more.
"""

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from txrisk.scoring.metrics import MODEL_LOAD_FAILURES, Metrics

if TYPE_CHECKING:  # the bundle module imports the scoring package
    from txrisk.models.bundle import ModelBundle

log = logging.getLogger(__name__)

MAX_SLOW_CALLS = 8
_T = TypeVar("_T")
_pool = ThreadPoolExecutor(max_workers=MAX_SLOW_CALLS, thread_name_prefix="txrisk-model")


class ModelTimeout(Exception):
    """Scoring with the Champion model took longer than its timeout."""


@dataclass(frozen=True)
class ModelHealth:
    degraded: bool
    # Why, when degraded: e.g. "model bundle unavailable: FileNotFoundError: ...".
    reason: str | None = None


class Champion:
    def __init__(
        self,
        load: "Callable[[], ModelBundle]",
        *,
        timeout: float | None = None,
        retry_after: float = 30.0,
        metrics: Metrics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`load` returns the bundle or raises; `timeout` (seconds) bounds one scoring call, and
        None lets it run as long as it takes."""
        self._load = load
        self.timeout = timeout
        self._retry_after = retry_after
        self._metrics = metrics
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._bundle: ModelBundle | None = None
        self._reason: str | None = None
        self._retry_at = 0.0
        self._recovered = False

    @classmethod
    def of(cls, bundle: "ModelBundle", **options: object) -> "Champion":
        """A Champion over a bundle already loaded."""
        return cls(lambda: bundle, **options)  # type: ignore[arg-type]

    @classmethod
    def from_folder(cls, folder: Path, **options: object) -> "Champion":
        """A Champion that loads the bundle saved in `folder` the first time it is needed, and
        again after a failed load, so a fixed bundle takes over without a restart."""

        def load() -> "ModelBundle":
            from txrisk.models.bundle import ModelBundle

            return ModelBundle.load(folder)

        return cls(load, **options)  # type: ignore[arg-type]

    def switch(self, load: "Callable[[], ModelBundle]") -> None:
        """Score with the bundle `load` returns from now on, loaded at the next `acquire`. A
        Promotion or rollback does this; a model that was down is tried at once, not after its
        pause, and Degraded mode ends with the first Transaction the new one scores."""
        with self._lock:
            self._load = load
            self._bundle = None
            self._retry_at = 0.0

    def acquire(self) -> "tuple[ModelBundle | None, str | None]":
        """The bundle to score with, or None and why not: the model is down or its breaker is
        open. Loads the bundle if it is not loaded and a retry is due."""
        with self._lock:
            if self._reason is not None and self._monotonic() < self._retry_at:
                return None, self._reason
            if self._bundle is None:
                try:
                    self._bundle = self._load()
                except Exception as e:  # missing, corrupt, wrong type: all mean unavailable
                    log.warning("the model bundle could not be loaded: %r", e)
                    if self._metrics is not None:
                        self._metrics.increment(MODEL_LOAD_FAILURES)
                    self._open(f"model bundle unavailable: {type(e).__name__}: {e}")
                    return None, self._reason
            return self._bundle, None

    @property
    def loaded(self) -> "ModelBundle | None":
        """The bundle if it is loaded; never loads."""
        with self._lock:
            return self._bundle

    def failed(self, reason: str) -> None:
        """Scoring with the bundle timed out or raised: skip the model for a while."""
        with self._lock:
            self._open(f"model scoring failed: {reason}")

    def succeeded(self) -> None:
        """The model scored a Transaction: Degraded mode ends."""
        with self._lock:
            if self._reason is not None:
                self._reason = None
                self._recovered = True

    def health(self) -> ModelHealth:
        """Whether the model is usable now, trying a due bundle load but never a scoring call."""
        self.acquire()
        with self._lock:
            return ModelHealth(degraded=self._reason is not None, reason=self._reason)

    def take_recovery(self) -> bool:
        """True once after Degraded mode ended: the cue to re-score what it left behind."""
        with self._lock:
            recovered, self._recovered = self._recovered, False
            return recovered

    def call(self, work: Callable[[], _T]) -> _T:
        """Run `work` (a scoring call with the bundle) within the timeout, or raise
        `ModelTimeout`; whatever `work` raises is raised."""
        if self.timeout is None:
            return work()
        future = _pool.submit(work)
        try:
            return future.result(timeout=self.timeout)
        except FutureTimeout:
            future.cancel()  # only helps if it never started; a running call is left to finish
            raise ModelTimeout(f"no answer within {self.timeout * 1000:.0f} ms") from None

    def _open(self, reason: str) -> None:
        self._reason = reason
        self._retry_at = self._monotonic() + self._retry_after
