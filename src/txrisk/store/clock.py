"""The simulation clock (ADR 0005), kept in Postgres so every worker, the API and the scheduler
read one clock.

It is the latest event time any of them has seen, and it only moves forward. It is the only source
of "now" for business logic; in a real deployment it would simply track the wall clock.
"""

from datetime import datetime

import sqlalchemy as sa

from txrisk.store.tables import simulation_clock


class ClockNotStarted(LookupError):
    """No event has been seen yet, so the clock has no reading."""


class SimulationClock:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    def advance(self, to: datetime) -> bool:
        """Move the clock to `to` if that is later than its reading; True if it moved.

        A single conditional UPDATE: a call that would not move the clock does not lock the row,
        so many workers advancing it do not wait on each other.
        """
        with self._engine.begin() as connection:
            moved = connection.execute(
                sa.update(simulation_clock)
                .where(
                    simulation_clock.c.singleton,
                    sa.or_(simulation_clock.c.at.is_(None), simulation_clock.c.at < to),
                )
                .values(at=to)
            )
        return moved.rowcount > 0

    def peek(self) -> datetime | None:
        """The clock's reading, or None before any event."""
        with self._engine.connect() as connection:
            reading: datetime | None = connection.execute(
                sa.select(simulation_clock.c.at)
            ).scalar_one()
        return reading

    def now(self) -> datetime:
        """The clock's reading; raises `ClockNotStarted` before any event."""
        reading = self.peek()
        if reading is None:
            raise ClockNotStarted("no event has been seen yet")
        return reading
