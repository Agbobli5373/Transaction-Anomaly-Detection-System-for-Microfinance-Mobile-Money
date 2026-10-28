"""The scheduler: jobs fire once per simulated period as the simulation clock advances."""

import json
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from conftest import _require_docker
from pydantic import ValidationError

from txrisk.cli import main
from txrisk.scheduler import (
    ComposeLauncher,
    ComposeSettings,
    JobSpec,
    KubernetesLauncher,
    KubernetesSettings,
    LaunchFailed,
    RestKubernetesApi,
    Schedule,
    Scheduler,
    job_manifest,
    load_scheduler_config,
    period_of,
    run_name,
)
from txrisk.store import SimulationClock, create_engine

SCHEDULER_CONFIG = Path(__file__).resolve().parents[1] / "config" / "scheduler.toml"
# A Monday.
MONDAY = datetime(2026, 4, 6, 9, 30, tzinfo=UTC)
DAY = timedelta(days=1)
PURGE = JobSpec(name="retention-purge", schedule=Schedule.DAILY, command=("txrisk", "purge"))
RETRAIN = JobSpec(name="retrain", schedule=Schedule.WEEKLY, command=("txrisk", "train"))


class FakeLauncher:
    """Records what it was asked to launch; can be made to fail."""

    def __init__(self) -> None:
        self.launched: list[tuple[str, date]] = []
        self.failing: set[str] = set()
        self.delay = 0.0

    def launch(self, job: JobSpec, period: date) -> str:
        if job.name in self.failing:
            raise LaunchFailed(f"{job.name} would not start")
        time.sleep(self.delay)
        self.launched.append((job.name, period))
        return run_name(job, period)


@pytest.fixture
def engine(database: str) -> sa.Engine:
    return create_engine(database)


@pytest.fixture
def clock(engine: sa.Engine) -> SimulationClock:
    return SimulationClock(engine)


def scheduler(
    engine: sa.Engine, clock: SimulationClock, launcher: FakeLauncher, *jobs: JobSpec
) -> Scheduler:
    return Scheduler(engine, clock, launcher, jobs)


def test_nothing_fires_before_the_clock_starts(engine: sa.Engine, clock: SimulationClock):
    launcher = FakeLauncher()

    assert scheduler(engine, clock, launcher, PURGE).tick() == []
    assert launcher.launched == []


def test_a_daily_job_fires_once_a_simulated_day_however_often_the_clock_is_checked(
    engine: sa.Engine, clock: SimulationClock
):
    launcher = FakeLauncher()
    schedule = scheduler(engine, clock, launcher, PURGE)

    for hours in (0, 1, 5, 9, 23):
        clock.advance(MONDAY.replace(hour=0, minute=0) + timedelta(hours=hours))
        schedule.tick()
        schedule.tick()
    clock.advance(MONDAY + DAY)
    schedule.tick()

    assert launcher.launched == [
        ("retention-purge", date(2026, 4, 6)),
        ("retention-purge", date(2026, 4, 7)),
    ]


def test_a_weekly_job_fires_when_the_clock_crosses_into_a_new_week(
    engine: sa.Engine, clock: SimulationClock
):
    launcher = FakeLauncher()
    schedule = scheduler(engine, clock, launcher, RETRAIN)

    for days in range(0, 15):  # Monday to Monday to Monday
        clock.advance(MONDAY + days * DAY)
        schedule.tick()

    assert launcher.launched == [
        ("retrain", date(2026, 4, 6)),
        ("retrain", date(2026, 4, 13)),
        ("retrain", date(2026, 4, 20)),
    ]


def test_each_job_keeps_its_own_schedule(engine: sa.Engine, clock: SimulationClock):
    launcher = FakeLauncher()
    schedule = scheduler(engine, clock, launcher, PURGE, RETRAIN)

    for days in range(8):
        clock.advance(MONDAY + days * DAY)
        schedule.tick()

    assert [name for name, _ in launcher.launched].count("retention-purge") == 8
    assert [name for name, _ in launcher.launched].count("retrain") == 2


def test_a_restart_does_not_fire_a_period_again(engine: sa.Engine, clock: SimulationClock):
    launcher = FakeLauncher()
    clock.advance(MONDAY)
    scheduler(engine, clock, launcher, PURGE, RETRAIN).tick()

    # A new process, with nothing in memory.
    restarted = scheduler(engine, clock, launcher, PURGE, RETRAIN)
    assert restarted.tick() == []
    clock.advance(MONDAY + DAY)
    restarted.tick()

    assert launcher.launched == [
        ("retention-purge", date(2026, 4, 6)),
        ("retrain", date(2026, 4, 6)),
        ("retention-purge", date(2026, 4, 7)),
    ]


def test_periods_skipped_while_stopped_are_not_made_up(engine: sa.Engine, clock: SimulationClock):
    launcher = FakeLauncher()
    clock.advance(MONDAY)
    scheduler(engine, clock, launcher, PURGE).tick()

    clock.advance(MONDAY + 5 * DAY)  # the scheduler was down for five simulated days
    scheduler(engine, clock, launcher, PURGE).tick()

    assert launcher.launched == [
        ("retention-purge", date(2026, 4, 6)),
        ("retention-purge", date(2026, 4, 11)),
    ]


def test_a_job_that_fails_to_launch_is_retried_and_does_not_hold_back_the_others(
    engine: sa.Engine, clock: SimulationClock
):
    launcher = FakeLauncher()
    launcher.failing = {"retention-purge"}
    clock.advance(MONDAY)
    schedule = scheduler(engine, clock, launcher, PURGE, RETRAIN)

    first = schedule.tick()
    launcher.failing = set()
    second = schedule.tick()
    third = schedule.tick()

    assert [f.job for f in first] == ["retrain"]
    assert [f.job for f in second] == ["retention-purge"]
    assert third == []


def test_two_schedulers_running_at_once_launch_a_period_once(
    engine: sa.Engine, clock: SimulationClock
):
    launcher = FakeLauncher()
    launcher.delay = 0.2
    clock.advance(MONDAY)
    schedulers = [scheduler(engine, clock, launcher, PURGE) for _ in range(4)]

    threads = [threading.Thread(target=s.tick) for s in schedulers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert launcher.launched == [("retention-purge", date(2026, 4, 6))]


def test_a_fired_job_records_the_period_the_simulation_clock_and_what_started(
    engine: sa.Engine, clock: SimulationClock
):
    clock.advance(MONDAY)

    [fired] = scheduler(engine, clock, FakeLauncher(), RETRAIN).tick()

    assert (fired.job, fired.period, fired.at) == ("retrain", date(2026, 4, 6), MONDAY)
    with engine.connect() as connection:
        row = connection.execute(
            sa.text("SELECT period, fired_at, launch_ref FROM scheduled_jobs")
        ).one()
    assert tuple(row) == (date(2026, 4, 6), MONDAY, "txrisk-retrain-20260406")


def test_run_ticks_until_told_to_stop(engine: sa.Engine, clock: SimulationClock):
    launcher = FakeLauncher()
    clock.advance(MONDAY)
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(MONDAY + DAY * len(waits))

    scheduler(engine, clock, launcher, PURGE).run(lambda: len(waits) >= 3, sleep, poll_seconds=0.5)

    assert waits == [0.5, 0.5, 0.5]
    assert [period for _, period in launcher.launched] == [
        date(2026, 4, 6),
        date(2026, 4, 7),
        date(2026, 4, 8),
    ]


def test_a_period_is_a_day_or_a_week_in_the_configured_timezone():
    late_sunday_utc = datetime(2026, 4, 5, 23, 30, tzinfo=UTC)

    assert period_of(Schedule.DAILY, late_sunday_utc, ZoneInfo("UTC")) == date(2026, 4, 5)
    assert period_of(Schedule.WEEKLY, late_sunday_utc, ZoneInfo("UTC")) == date(2026, 3, 30)
    # It is already Monday in Auckland.
    assert period_of(Schedule.DAILY, late_sunday_utc, ZoneInfo("Pacific/Auckland")) == date(
        2026, 4, 6
    )
    assert period_of(Schedule.WEEKLY, late_sunday_utc, ZoneInfo("Pacific/Auckland")) == date(
        2026, 4, 6
    )


def test_the_shipped_scheduler_config_fires_the_retention_purge_daily():
    config = load_scheduler_config(SCHEDULER_CONFIG)

    [purge] = [job for job in config.jobs if job.name == "retention-purge"]
    assert (purge.schedule, purge.command) == (Schedule.DAILY, ("txrisk", "purge"))


def test_a_job_name_that_could_not_be_a_container_name_is_refused():
    with pytest.raises(ValidationError):
        JobSpec(name="Retention Purge", schedule=Schedule.DAILY, command=("txrisk",))


def test_txrisk_scheduler_once_before_the_clock_starts_fires_nothing(
    database: str, capsys: pytest.CaptureFixture[str]
):
    exit_code = main(
        ["scheduler", "--once", "--database-url", database, "--config", str(SCHEDULER_CONFIG)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""


# --- The compose launcher ---------------------------------------------------------------------


def test_the_compose_launcher_runs_the_job_as_a_detached_one_off_of_its_service():
    calls: list[Sequence[str]] = []

    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    launcher = ComposeLauncher(ComposeSettings(file=Path("stack.yml"), service="job"), run=run)

    ref = launcher.launch(PURGE, date(2026, 4, 6))

    assert ref == "txrisk-retention-purge-20260406"
    assert calls == [
        [
            *("docker", "compose", "--file", "stack.yml", "run", "--detach", "--rm", "--no-deps"),
            *("--name", "txrisk-retention-purge-20260406", "job", "txrisk", "purge"),
        ]
    ]


def test_the_compose_launcher_reports_a_failed_launch():
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, "", "no such service: job")

    with pytest.raises(LaunchFailed, match="no such service"):
        ComposeLauncher(ComposeSettings(), run=run).launch(PURGE, date(2026, 4, 6))


def test_the_compose_launcher_takes_a_container_that_already_exists_as_launched():
    def run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "Conflict. The container name is already in use"
        )

    ref = ComposeLauncher(ComposeSettings(), run=run).launch(PURGE, date(2026, 4, 6))

    assert ref == "txrisk-retention-purge-20260406"


@pytest.fixture
def compose_file(tmp_path: Path) -> Iterator[Path]:
    _require_docker()
    file = tmp_path / "compose.yml"
    file.write_text(
        "services:\n  job:\n    image: postgres:17-alpine\n    command: ['sleep', '60']\n",
        encoding="utf-8",
    )
    yield file
    subprocess.run(["docker", "rm", "--force", "txrisk-sleeper-20260406"], capture_output=True)


def test_the_compose_launcher_starts_a_real_one_off_container(compose_file: Path):
    job = JobSpec(name="sleeper", schedule=Schedule.DAILY, command=("sleep", "60"))

    ref = ComposeLauncher(ComposeSettings(file=compose_file, service="job")).launch(
        job, date(2026, 4, 6)
    )

    running = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}} {{.Config.Cmd}}", ref],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert running.startswith("true")
    assert "sleep 60" in running


# --- The Kubernetes launcher ------------------------------------------------------------------

KUBERNETES = KubernetesSettings(
    namespace="txrisk", image="registry.example/txrisk:1", env_from="txrisk-env"
)


def test_the_kubernetes_job_runs_the_command_once_in_the_configured_image():
    manifest = job_manifest(KUBERNETES, RETRAIN, date(2026, 4, 6))

    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == "txrisk-retrain-20260406"
    assert manifest["metadata"]["namespace"] == "txrisk"
    assert manifest["metadata"]["labels"]["txrisk/period"] == "2026-04-06"
    assert manifest["spec"]["backoffLimit"] == 0
    pod = manifest["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    [container] = pod["containers"]
    assert container["image"] == "registry.example/txrisk:1"
    assert container["command"] == ["txrisk", "train"]


def test_the_kubernetes_job_takes_its_environment_from_a_secret_by_name_only():
    manifest = job_manifest(KUBERNETES, RETRAIN, date(2026, 4, 6))

    [container] = manifest["spec"]["template"]["spec"]["containers"]
    assert container["envFrom"] == [{"secretRef": {"name": "txrisk-env"}}]
    assert "env" not in container


def test_the_kubernetes_job_carries_plain_settings_the_models_volume_and_a_user():
    settings = KubernetesSettings(
        env_from="txrisk-env",
        env={"TXRISK_MODELS_DIR": "/models", "TXRISK_REDIS_URL": "redis://redis:6379/0"},
        models_claim="models",
        run_as_user=10001,
    )

    pod = job_manifest(settings, RETRAIN, date(2026, 4, 6))["spec"]["template"]["spec"]

    [container] = pod["containers"]
    assert container["env"] == [
        {"name": "TXRISK_MODELS_DIR", "value": "/models"},
        {"name": "TXRISK_REDIS_URL", "value": "redis://redis:6379/0"},
    ]
    assert container["volumeMounts"] == [{"name": "models", "mountPath": "/models"}]
    assert pod["volumes"][0]["persistentVolumeClaim"] == {"claimName": "models"}
    assert pod["securityContext"] == {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001}


def test_a_deployments_kubernetes_table_replaces_the_one_in_the_job_config(tmp_path: Path):
    deployment = tmp_path / "kubernetes.toml"
    deployment.write_text('[kubernetes]\nnamespace = "txrisk"\nimage = "txrisk:ci"\n')

    config = load_scheduler_config(SCHEDULER_CONFIG, deployment)

    assert (config.kubernetes.namespace, config.kubernetes.image) == ("txrisk", "txrisk:ci")
    assert config.jobs == load_scheduler_config(SCHEDULER_CONFIG).jobs


class FakeKubernetes:
    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}

    def create_job(self, namespace: str, manifest: dict[str, Any]) -> bool:
        name = f"{namespace}/{manifest['metadata']['name']}"
        if name in self.jobs:
            return False
        self.jobs[name] = manifest
        return True


def test_the_kubernetes_launcher_creates_one_job_per_period():
    api = FakeKubernetes()
    launcher = KubernetesLauncher(KUBERNETES, api)

    first = launcher.launch(RETRAIN, date(2026, 4, 6))
    again = launcher.launch(RETRAIN, date(2026, 4, 6))  # a crash window re-fire
    next_week = launcher.launch(RETRAIN, date(2026, 4, 13))

    assert first == again == "txrisk-retrain-20260406"
    assert sorted(api.jobs) == ["txrisk/txrisk-retrain-20260406", "txrisk/txrisk-retrain-20260413"]
    assert next_week == "txrisk-retrain-20260413"


def test_the_scheduler_launches_kubernetes_jobs_as_the_clock_advances(
    engine: sa.Engine, clock: SimulationClock
):
    api = FakeKubernetes()
    schedule = Scheduler(engine, clock, KubernetesLauncher(KUBERNETES, api), (RETRAIN,))

    for days in range(10):
        clock.advance(MONDAY + days * DAY)
        schedule.tick()

    assert sorted(api.jobs) == ["txrisk/txrisk-retrain-20260406", "txrisk/txrisk-retrain-20260413"]


class FakeApiServer:
    """A Kubernetes API that records the requests it gets and answers with a set status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.requests: list[tuple[str, str | None, dict[str, Any]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, self.headers["Authorization"], body))
                self.send_response(outer.status)
                self.end_headers()
                self.wfile.write(b'{"kind": "Status"}')

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def api_server(request: pytest.FixtureRequest) -> Iterator[FakeApiServer]:
    server = FakeApiServer(getattr(request, "param", 201))
    server.thread.start()
    yield server
    server.server.shutdown()
    server.server.server_close()


def rest_api(server: FakeApiServer, tmp_path: Path, bearer: str) -> RestKubernetesApi:
    mounted = tmp_path / "mounted"
    mounted.write_text(bearer + "\n", encoding="utf-8")
    return RestKubernetesApi(server.url, mounted)


def test_the_rest_api_posts_the_job_with_the_service_accounts_bearer(
    api_server: FakeApiServer, tmp_path: Path
):
    bearer = uuid4().hex
    manifest = job_manifest(KUBERNETES, RETRAIN, date(2026, 4, 6))

    created = rest_api(api_server, tmp_path, bearer).create_job("txrisk", manifest)

    assert created is True
    [(path, authorization, body)] = api_server.requests
    assert path == "/apis/batch/v1/namespaces/txrisk/jobs"
    assert authorization == f"Bearer {bearer}"
    assert body == manifest


@pytest.mark.parametrize("api_server", [409], indirect=True)
def test_the_rest_api_reports_an_existing_job_as_not_created(
    api_server: FakeApiServer, tmp_path: Path
):
    api = rest_api(api_server, tmp_path, uuid4().hex)

    assert api.create_job("txrisk", job_manifest(KUBERNETES, RETRAIN, date(2026, 4, 6))) is False


@pytest.mark.parametrize("api_server", [403], indirect=True)
def test_the_rest_api_reports_a_refused_job_as_a_failed_launch(
    api_server: FakeApiServer, tmp_path: Path
):
    api = rest_api(api_server, tmp_path, uuid4().hex)

    with pytest.raises(LaunchFailed, match="403"):
        api.create_job("txrisk", job_manifest(KUBERNETES, RETRAIN, date(2026, 4, 6)))
