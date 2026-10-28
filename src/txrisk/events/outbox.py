"""Publishing the transactional outbox to Redpanda.

A change that must announce an event (a Case closed as fraud) writes the event to the `outbox`
table in the same database transaction. This publishes what is waiting: it reads a batch with
`FOR UPDATE SKIP LOCKED` (so several publishers share the work), sends it through the idempotent
`Publisher`, waits until the broker confirms every event, and only then marks the rows published.
A crash before the mark republishes the batch, so delivery is at least once and consumers must be
idempotent, as the Fraud label store is (the first label stands).
"""

import logging
from collections.abc import Callable

import sqlalchemy as sa

from txrisk.events.stream import Publisher, Topics
from txrisk.store.tables import outbox

log = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(
        self,
        engine: sa.Engine,
        publisher: Publisher,
        *,
        topics: Topics | None = None,
        batch_size: int = 500,
    ) -> None:
        self._engine = engine
        self._publisher = publisher
        self._topics = topics or Topics()
        self._batch_size = batch_size

    def publish_pending(self) -> int:
        """Publish one batch of waiting events, oldest first; returns how many were sent.

        Raises `DeliveryFailed` if the broker does not confirm them, leaving them waiting."""
        with self._engine.begin() as connection:
            rows = connection.execute(
                sa.select(outbox.c.outbox_id, outbox.c.topic, outbox.c.key, outbox.c.payload)
                .where(~outbox.c.published)
                .order_by(outbox.c.outbox_id)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            if not rows:
                return 0
            for row in rows:
                self._publisher.publish_raw(
                    self._topics.prefix + row.topic, row.key.encode(), row.payload.encode()
                )
            self._publisher.flush()
            connection.execute(
                sa.update(outbox)
                .where(outbox.c.outbox_id.in_([r.outbox_id for r in rows]))
                .values(published=True)
            )
        return len(rows)

    def run(
        self,
        should_stop: Callable[[], bool],
        wait: Callable[[float], object],
        *,
        poll_seconds: float,
    ) -> None:
        """Publish until told to stop, waiting `poll_seconds` between empty checks."""
        while not should_stop():
            try:
                sent = self.publish_pending()
            except Exception:
                log.exception("publishing the outbox failed; will retry")
                sent = 0
            if sent:
                log.info("published %d events from the outbox", sent)
            else:
                wait(poll_seconds)
