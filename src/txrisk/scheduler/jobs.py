"""The scheduled jobs and how the scheduler is configured, from `config/scheduler.toml`."""

import tomllib
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Schedule(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"


# Lower-case DNS label characters, as a job's name is part of its container or Kubernetes Job name.
JobName = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$", max_length=40)
]


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: JobName
    schedule: Schedule
    # The command a one-off container runs, e.g. ["txrisk", "purge"].
    command: tuple[str, ...] = Field(min_length=1)


class ComposeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    file: Path = Path("docker-compose.yml")
    # The compose service whose definition (image, env, volumes) every job container uses.
    service: str = "job"


class KubernetesSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = "default"
    image: str = "txrisk:dev"
    # A Secret whose entries become the job's environment, so no credential is in the Job spec.
    env_from: str | None = None
    service_account: str | None = None
    backoff_limit: int = Field(default=0, ge=0)
    ttl_seconds_after_finished: int = Field(default=3600, ge=0)


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Where a day or week starts. Ghana is UTC+0 all year.
    timezone: str = "UTC"
    jobs: tuple[JobSpec, ...] = ()
    compose: ComposeSettings = ComposeSettings()
    kubernetes: KubernetesSettings = KubernetesSettings()

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("jobs")
    @classmethod
    def _unique_names(cls, jobs: tuple[JobSpec, ...]) -> tuple[JobSpec, ...]:
        names = [job.name for job in jobs]
        if len(set(names)) != len(names):
            raise ValueError("job names must be unique")
        return jobs


def load_scheduler_config(path: Path) -> SchedulerConfig:
    """Read the config; each `[jobs.<name>]` table is a job named `<name>`."""
    with path.open("rb") as f:
        raw = tomllib.load(f)
    raw["jobs"] = [{"name": name, **job} for name, job in raw.get("jobs", {}).items()]
    return SchedulerConfig.model_validate(raw)


def period_of(schedule: Schedule, moment: datetime, timezone: ZoneInfo) -> date:
    """The first day of the daily or weekly period `moment` falls in; a week starts on Monday."""
    day = moment.astimezone(timezone).date()
    if schedule is Schedule.WEEKLY:
        return day - timedelta(days=day.weekday())
    return day
