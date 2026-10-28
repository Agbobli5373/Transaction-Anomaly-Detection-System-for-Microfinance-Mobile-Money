"""The development Keycloak: it refuses to start without real demo sign-in values, and compose
keeps each service to the settings it needs."""

import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import _need_docker, keycloak_container

from txrisk.api.signin import demo_signin_env

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("analyst", "supervisor", "admin")
DEMO_SIGN_INS = {demo_signin_env(role) for role in ROLES}


def _exit_of(environment: dict[str, str]) -> tuple[int, str]:
    """How the compose Keycloak container ends with `environment`, and what it said."""
    _need_docker()
    container = keycloak_container()
    for name, value in environment.items():
        container.with_env(name, value)
    with container:
        wrapped = container.get_wrapped_container()
        deadline = time.monotonic() + 60
        while True:
            wrapped.reload()
            if wrapped.status == "exited" or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        stdout, stderr = container.get_logs()
        return int(wrapped.attrs["State"]["ExitCode"]), (stdout + stderr).decode()


@pytest.mark.parametrize(
    ("environment", "complaint"),
    [
        ({}, "TXRISK_DEMO_SIGNIN_ADMIN is not set"),
        (
            {"TXRISK_DEMO_SIGNIN_ANALYST": "a" * 16, "TXRISK_DEMO_SIGNIN_SUPERVISOR": "s" * 16},
            "TXRISK_DEMO_SIGNIN_ADMIN is not set",
        ),
        (
            {name: "x" * 16 for name in DEMO_SIGN_INS} | {"TXRISK_DEMO_SIGNIN_ADMIN": "short"},
            "TXRISK_DEMO_SIGNIN_ADMIN is shorter than 12 characters",
        ),
        (
            {name: "x" * 16 for name in DEMO_SIGN_INS}
            | {"TXRISK_DEMO_SIGNIN_ADMIN": "${TXRISK_DEMO_SIGNIN_ADMIN}"},
            "TXRISK_DEMO_SIGNIN_ADMIN holds a placeholder",
        ),
    ],
    ids=["none set", "admin unset", "admin too short", "admin a placeholder"],
)
def test_keycloak_refuses_to_start_without_a_real_sign_in_for_every_demo_user(
    environment: dict[str, str], complaint: str
):
    """Otherwise Keycloak would import the realm keeping the literal placeholder as the demo
    user's credential: a publicly known Admin sign-in."""
    exit_code, said = _exit_of(environment)

    assert exit_code == 1
    assert complaint in said
    assert "Listening on" not in said  # Keycloak itself never started


def _compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    return loaded


def _keys(env_file: Path) -> set[str]:
    lines = env_file.read_text().splitlines()
    return {line.split("=", 1)[0] for line in lines if "=" in line and not line.startswith("#")}


def test_compose_keycloak_reads_only_its_own_settings_and_only_on_this_machine():
    keycloak = _compose()["services"]["keycloak"]

    assert keycloak["ports"] == ["127.0.0.1:8080:8080"]
    assert keycloak["entrypoint"] == ["bash", "/opt/txrisk/start-dev.sh"]
    assert [f["path"] for f in keycloak["env_file"]] == ["keycloak.env"]


def test_the_api_never_sees_keycloaks_settings():
    compose = _compose()
    api = compose["services"]["api"]

    assert api["env_file"] == ".env"
    assert not _keys(ROOT / ".env.example") & _keys(ROOT / "keycloak.env.example")
    assert _keys(ROOT / "keycloak.env.example") >= DEMO_SIGN_INS
    assert not any(k.startswith(("KC_", "TXRISK_DEMO_")) for k in _keys(ROOT / ".env.example"))
    assert not any(k.startswith(("KC_", "TXRISK_DEMO_")) for k in api["environment"])
