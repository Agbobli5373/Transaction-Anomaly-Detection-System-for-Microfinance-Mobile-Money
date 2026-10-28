"""The Challenger model as scoring sees it: whichever bundle retraining last named, followed
without a restart.

Retraining saves a bundle under the models folder and then names its version in
`challenger_model`. Every process that scores polls that row, and loads the bundle the first time
it sees a new version, so a Challenger registered while workers run is scoring in shadow within
`refresh_seconds`. A bundle that will not load leaves the process on the one it had (or none) and
is tried again after the next poll; it never stops scoring, which is the Champion's business.

How often to poll is operational plumbing, so it reads a monotonic clock, not the simulation
clock: it never decides what a Transaction scores, only when the row is read.
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

from txrisk.models.bundle import ModelBundle
from txrisk.store.tables import challenger_model

log = logging.getLogger(__name__)


def challenger_version(engine: sa.Engine) -> str | None:
    """The bundle version named as the Challenger model, if any."""
    with engine.connect() as connection:
        version: str | None = connection.execute(
            sa.select(challenger_model.c.bundle_version)
        ).scalar_one()
    return version


class ChallengerModel:
    def __init__(
        self,
        engine: sa.Engine,
        models: Path,
        *,
        refresh_seconds: float = 5.0,
        load: Callable[[Path], ModelBundle] = ModelBundle.load,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`models` is the folder holding each bundle as `<version>/`, as `txrisk train` and
        `txrisk retrain` write them."""
        self._engine = engine
        self._models = models
        self._refresh_seconds = refresh_seconds
        self._load = load
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._bundle: ModelBundle | None = None
        self._next_poll = 0.0

    def current(self) -> ModelBundle | None:
        """The Challenger model's bundle, or None if there is none (yet). Polls Postgres when a
        poll is due, so a new Challenger is picked up here, not on a restart."""
        with self._lock:
            if self._monotonic() >= self._next_poll:
                self._next_poll = self._monotonic() + self._refresh_seconds
                self._refresh()
            return self._bundle

    def _refresh(self) -> None:
        try:
            version = challenger_version(self._engine)
        except sa.exc.SQLAlchemyError:
            log.warning("could not read the Challenger model", exc_info=True)
            return  # keep scoring with the one already loaded
        if version is None:
            self._bundle = None
        elif self._bundle is None or self._bundle.version != version:
            try:
                bundle = self._load(self._models / version)
            except Exception:  # missing, corrupt or wrong type: tried again at the next poll
                log.warning("could not load the Challenger model %s", version, exc_info=True)
                return
            log.info("Challenger model is now %s", version)
            self._bundle = bundle
