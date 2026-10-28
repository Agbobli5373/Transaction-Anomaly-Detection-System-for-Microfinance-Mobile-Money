"""Counters the scoring path emits, behind a small interface.

There is no metrics library yet: `InMemoryMetrics` counts in-process (and is what tests read).
The observability ticket (#20) implements `Metrics` over Prometheus counters and passes it in;
nothing here changes then.
"""

import threading
from collections import Counter
from typing import Protocol

# An assessment was made with Rule hits alone because the Champion model was unavailable, too
# slow or failed. Labelled `reason`: unavailable, timeout or error.
DEGRADED_ASSESSMENTS = "txrisk_degraded_assessments_total"
# A Degraded Transaction was re-scored by the full model.
RESCORED = "txrisk_rescored_total"
# Loading the Champion model bundle failed.
MODEL_LOAD_FAILURES = "txrisk_model_load_failures_total"


class Metrics(Protocol):
    def increment(self, name: str, **labels: str) -> None:
        """Add one to the counter `name` with these labels."""
        ...


class InMemoryMetrics:
    """Counters held in this process, safe to increment from several threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()

    def increment(self, name: str, **labels: str) -> None:
        with self._lock:
            self._counts[(name, tuple(sorted(labels.items())))] += 1

    def value(self, name: str, **labels: str) -> int:
        """The count for exactly these labels."""
        with self._lock:
            return self._counts[(name, tuple(sorted(labels.items())))]

    def total(self, name: str) -> int:
        """The count over every label set."""
        with self._lock:
            return sum(n for (counter, _), n in self._counts.items() if counter == name)
