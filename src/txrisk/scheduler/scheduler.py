"""The scheduler: fires each job once per simulated period, as the simulation clock crosses it.

The clock is event time (ADR 0005), so during a replay a "daily" job fires every few wall-clock
minutes. The period each job last fired for is kept in Postgres, so a restart, or a second
scheduler running, never fires a period twice.

A job fires for the period the clock is in now. If the clock jumped over whole periods (a restart
after a long stop, a fast replay), the skipped ones are not made up: these jobs work from the
data as it is, so one run now does what the missed runs would have.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from txrisk.observability.metrics import Metrics
from txrisk.scheduler.jobs import JobSpec, period_of
from txrisk.scheduler.launcher import JobLauncher
from txrisk.scoring import Clock
from txrisk.store.clock import ClockNotStarted
from txrisk.store.tables import scheduled_jobs

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Fired:
    job: str
    period: date
    # The simulation clock when it fired.
    at: datetime
    launch_ref: str


class Scheduler:
    def __init__(
        self,
        engine: sa.Engine,
        clock: Clock,
        launcher: JobLauncher,
        jobs: tuple[JobSpec, ...],
        *,
        timezone: ZoneInfo | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._launcher = launcher
        self._jobs = jobs
        self._timezone = timezone or ZoneInfo("UTC")
        self._metrics = metrics

    def tick(self) -> list[Fired]:
        """Launch every job whose period the clock has entered since it last fired.

        A job that fails to launch is logged and left to the next tick; the others still fire.
        """
        try:
            now = self._clock.now()
        except ClockNotStarted:
            return []
        fired = []
        for job in self._jobs:
            period = period_of(job.schedule, now, self._timezone)
            try:
                found = self._fire_if_due(job, period, now)
            except Exception:
                log.exception("could not launch %s for %s", job.name, period)
                continue
            if found is not None:
                fired.append(found)
                if self._metrics is not None:
                    self._metrics.scheduler_jobs.labels(job.name).inc()
                log.info("fired %s for %s as %s", job.name, period, found.launch_ref)
        return fired

    def run(
        self,
        should_stop: Callable[[], bool],
        sleep: Callable[[float], object],
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        """Tick until `should_stop()`, waiting `poll_seconds` of wall time between ticks."""
        while not should_stop():
            self.tick()
            sleep(poll_seconds)

    def _fire_if_due(self, job: JobSpec, period: date, now: datetime) -> Fired | None:
        last = self._last_period(job.name)
        if last is not None and last >= period:
            return None
        with self._engine.begin() as connection:
            connection.execute(
                insert(scheduled_jobs).values(job_name=job.name).on_conflict_do_nothing()
            )
            # Locked until commit, so a second scheduler waits here, then sees the period fired.
            last = connection.execute(
                sa.select(scheduled_jobs.c.period)
                .where(scheduled_jobs.c.job_name == job.name)
                .with_for_update()
            ).scalar_one()
            if last is not None and last >= period:
                return None
            # Recorded only if the launch succeeds: a failed launch is tried again next tick.
            ref = self._launcher.launch(job, period)
            connection.execute(
                sa.update(scheduled_jobs)
                .where(scheduled_jobs.c.job_name == job.name)
                .values(period=period, fired_at=now, launch_ref=ref)
            )
        return Fired(job=job.name, period=period, at=now, launch_ref=ref)

    def _last_period(self, job_name: str) -> date | None:
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(scheduled_jobs.c.period).where(scheduled_jobs.c.job_name == job_name)
            ).scalar_one_or_none()
