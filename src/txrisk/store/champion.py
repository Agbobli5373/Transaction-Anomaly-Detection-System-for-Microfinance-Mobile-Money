"""The Champion model as every process sees it: whichever bundle Promotion last named, followed
without a restart.

`champion_model` holds one row. Null means the bundle the process is configured with
(`TXRISK_MODEL_BUNDLE`), the first Champion; a Promotion or rollback sets it to a version saved
under the models folder. Each process polls the row, and switches the first time it sees a change,
so a Promotion reaches the workers and the API within `refresh_seconds`. A bundle that will not
load puts the process in Degraded mode, as any Champion that is down does, and is tried again.

How often to poll is operational plumbing, so it reads a monotonic clock, not the simulation
clock: it never decides what a Transaction scores, only when the row is read.
"""

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import sqlalchemy as sa

from txrisk.scoring.champion import Champion
from txrisk.store.tables import champion_model

if TYPE_CHECKING:
    from txrisk.models.bundle import ModelBundle

log = logging.getLogger(__name__)

_UNSET = object()


def champion_pointer(engine: sa.Engine) -> str | None:
    """The bundle version Promotion named as the Champion model; None while it is still the
    configured bundle."""
    with engine.connect() as connection:
        version: str | None = connection.execute(
            sa.select(champion_model.c.bundle_version)
        ).scalar_one()
    return version


def champion_folder(engine: sa.Engine, configured: Path | None, models: Path | None) -> Path | None:
    """Where the Champion model's bundle is saved: under `models` if Promotion named one, else
    the `configured` folder."""
    version = champion_pointer(engine)
    if version is None or models is None:
        return configured
    return models / version


class FollowedChampion(Champion):
    def __init__(
        self,
        engine: sa.Engine,
        *,
        configured: Path,
        models: Path,
        refresh_seconds: float = 5.0,
        load: "Callable[[Path], ModelBundle] | None" = None,
        poll_clock: Callable[[], float] = time.monotonic,
        **options: object,
    ) -> None:
        """`configured` is the first Champion's folder and `models` the folder holding every
        promoted bundle as `<version>/`. `options` are `Champion`'s (timeout, retry_after, ...)."""
        self._loader = load or _load_bundle
        super().__init__(lambda: self._loader(configured), **options)  # type: ignore[arg-type]
        self._engine = engine
        self._configured = configured
        self._models = models
        self._refresh_seconds = refresh_seconds
        self._poll_clock = poll_clock
        self._poll_lock = threading.Lock()
        self._next_poll = 0.0
        self._following: object = _UNSET

    def acquire(self) -> "tuple[ModelBundle | None, str | None]":
        self._follow()
        return super().acquire()

    def _follow(self) -> None:
        with self._poll_lock:
            if self._poll_clock() < self._next_poll:
                return
            self._next_poll = self._poll_clock() + self._refresh_seconds
            try:
                version = champion_pointer(self._engine)
            except sa.exc.SQLAlchemyError:
                log.warning("could not read the Champion model", exc_info=True)
                return  # keep scoring with the one already loaded
            if self._following is _UNSET:
                self._following = version
                if version is None:
                    return  # the configured bundle, already what loads
            elif version == self._following:
                return
            self._following = version
            folder = self._configured if version is None else self._models / version
            log.info("Champion model is now %s", version or "the configured bundle")
            self.switch(lambda: self._loader(folder))


def _load_bundle(folder: Path) -> "ModelBundle":
    from txrisk.models.bundle import ModelBundle

    return ModelBundle.load(folder)
