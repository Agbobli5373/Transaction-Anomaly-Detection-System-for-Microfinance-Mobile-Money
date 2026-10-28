"""Launching a job as a one-off container: through Docker Compose, or as a Kubernetes Job.

The scheduler only knows `JobLauncher`; each implementation starts the job's command and returns
at once, without waiting for it to finish.
"""

import json
import logging
import ssl
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol, Self

from txrisk.scheduler.jobs import ComposeSettings, JobSpec, KubernetesSettings

log = logging.getLogger(__name__)


class LaunchFailed(RuntimeError):
    """The job could not be started."""


class JobLauncher(Protocol):
    def launch(self, job: JobSpec, period: date) -> str:
        """Start `job` for the simulated `period`, and return a reference to what was started
        (a container name, a Kubernetes Job name). The name is derived from the job and period,
        so starting the same period again does not start a second copy where the platform can
        tell."""
        ...


def run_name(job: JobSpec, period: date) -> str:
    return f"txrisk-{job.name}-{period:%Y%m%d}"


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True)


class ComposeLauncher:
    """Runs the job as a detached one-off container of a compose service, so it has the
    service's image, environment and volumes (`docker compose run --detach`)."""

    def __init__(self, settings: ComposeSettings, *, run: Runner = _run) -> None:
        self._settings = settings
        self._run = run

    def launch(self, job: JobSpec, period: date) -> str:
        name = run_name(job, period)
        result = self._run(
            [
                "docker",
                "compose",
                "--file",
                str(self._settings.file),
                "run",
                "--detach",
                "--rm",
                "--no-deps",
                "--name",
                name,
                self._settings.service,
                *job.command,
            ]
        )
        if result.returncode != 0:
            if "already in use" in result.stderr:
                log.warning("%s was already launched", name)
                return name
            raise LaunchFailed(f"docker compose run failed: {result.stderr.strip()}")
        return name


class KubernetesApi(Protocol):
    def create_job(self, namespace: str, manifest: dict[str, Any]) -> bool:
        """Create the Job; False if one with that name already exists."""
        ...


class RestKubernetesApi:
    """The Kubernetes API over HTTP(S), as a pod authenticates to it with its service account:
    a bearer credential read from the mounted file on every call, so rotation is picked up."""

    def __init__(
        self,
        base_url: str,
        credential_file: Path,
        ca_file: Path | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential_file = credential_file
        self._context = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self._timeout = timeout

    @classmethod
    def in_cluster(cls, environ: Mapping[str, str]) -> Self:
        host, port = environ["KUBERNETES_SERVICE_HOST"], environ["KUBERNETES_SERVICE_PORT"]
        mounted = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        return cls(f"https://{host}:{port}", mounted / "token", mounted / "ca.crt")

    def create_job(self, namespace: str, manifest: dict[str, Any]) -> bool:
        request = urllib.request.Request(
            f"{self._base_url}/apis/batch/v1/namespaces/{namespace}/jobs",
            data=json.dumps(manifest).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._credential_file.read_text().strip(),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=self._context):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return False
            raise LaunchFailed(
                f"Kubernetes API answered {e.code}: {e.read().decode()[:500]}"
            ) from e
        except urllib.error.URLError as e:
            raise LaunchFailed(f"Kubernetes API unreachable: {e.reason}") from e


def job_manifest(settings: KubernetesSettings, job: JobSpec, period: date) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "job",
        "image": settings.image,
        "command": list(job.command),
    }
    if settings.env:
        container["env"] = [{"name": k, "value": v} for k, v in sorted(settings.env.items())]
    if settings.env_from:
        container["envFrom"] = [{"secretRef": {"name": settings.env_from}}]
    pod: dict[str, Any] = {"restartPolicy": "Never", "containers": [container]}
    if settings.models_claim:
        container["volumeMounts"] = [{"name": "models", "mountPath": "/models"}]
        pod["volumes"] = [
            {"name": "models", "persistentVolumeClaim": {"claimName": settings.models_claim}}
        ]
    if settings.run_as_user:
        pod["securityContext"] = {
            "runAsNonRoot": True,
            "runAsUser": settings.run_as_user,
            "fsGroup": settings.run_as_user,
        }
    if settings.service_account:
        pod["serviceAccountName"] = settings.service_account
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": run_name(job, period),
            "namespace": settings.namespace,
            "labels": {
                "app.kubernetes.io/name": "txrisk",
                "txrisk/job": job.name,
                "txrisk/period": period.isoformat(),
            },
        },
        "spec": {
            "backoffLimit": settings.backoff_limit,
            "ttlSecondsAfterFinished": settings.ttl_seconds_after_finished,
            "template": {"spec": pod},
        },
    }


class KubernetesLauncher:
    """Creates a Kubernetes Job named for the job and period. A Job of that name existing
    already means this period was launched, so it is left alone."""

    def __init__(self, settings: KubernetesSettings, api: KubernetesApi) -> None:
        self._settings = settings
        self._api = api

    def launch(self, job: JobSpec, period: date) -> str:
        manifest = job_manifest(self._settings, job, period)
        name = str(manifest["metadata"]["name"])
        if not self._api.create_job(self._settings.namespace, manifest):
            log.warning("Job %s already exists", name)
        return name
