import itertools
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from idp_helpers import LocalIdentityProvider, serve_identity_provider

from txrisk.api.signin import demo_signin_env
from txrisk.generator import GeneratorConfig, load_config
from txrisk.store import create_engine, migrate

if TYPE_CHECKING:
    from testcontainers.core.container import DockerContainer

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "generator.toml"
REALM_FOLDER = Path(__file__).resolve().parents[1] / "config" / "keycloak"
KEYCLOAK_START = Path(__file__).resolve().parents[1] / "deploy" / "keycloak" / "start-dev.sh"
# The same image compose runs.
KEYCLOAK_IMAGE = "keycloak/keycloak:26.4.2"
KEY = b"test-pseudonym-key"


@pytest.fixture(scope="session")
def default_config() -> GeneratorConfig:
    return load_config(DEFAULT_CONFIG)


@pytest.fixture(scope="session")
def small_config(default_config: GeneratorConfig) -> GeneratorConfig:
    """The default market at a size that generates in well under a second."""
    return default_config.model_copy(
        update={
            "period": default_config.period.model_copy(update={"days": 21}),
            "population": default_config.population.model_copy(
                update={"wallets": 300, "agents": 12, "merchants": 10}
            ),
        }
    )


REDPANDA_IMAGE = "redpandadata/redpanda:v24.2.18"


def _require_docker() -> None:
    """Skip the test without Docker locally; in CI, fail instead."""
    import docker

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        if os.environ.get("CI"):
            raise
        pytest.skip("Docker is not running")


@pytest.fixture(scope="session")
def postgres() -> Iterator[str]:
    """The URL of a throwaway Postgres server, run in Docker by Testcontainers.

    Without Docker the tests that need it are skipped locally, but fail in CI.
    """
    from testcontainers.community.postgres import PostgresContainer

    _require_docker()
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as container:
        yield container.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """The URL of a throwaway Redis server, run in Docker by Testcontainers.

    Tests share it and begin from `flush_redis`, which they ask for by name.
    """
    from testcontainers.community.redis import RedisContainer

    _require_docker()
    with RedisContainer("redis:7-alpine") as container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"


@pytest.fixture(scope="session")
def redpanda() -> Iterator[str]:
    """The bootstrap server of a throwaway Redpanda broker, run in Docker by Testcontainers.

    Tests share it, each under its own topic prefix.
    """
    from testcontainers.community.kafka import RedpandaContainer

    _require_docker()
    container = RedpandaContainer(REDPANDA_IMAGE)
    container.start(timeout=120)
    try:
        yield container.get_bootstrap_server()
    finally:
        container.stop()


def create_database(server_url: str, name: str, *, template: str | None = None) -> str:
    admin = sa.create_engine(server_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        clause = f" TEMPLATE {template}" if template else ""
        connection.execute(sa.text(f'CREATE DATABASE "{name}"{clause}'))
    admin.dispose()
    return sa.make_url(server_url).set(database=name).render_as_string(hide_password=False)


_databases = itertools.count()


@pytest.fixture(scope="session")
def migrated_template(postgres: str) -> str:
    """A migrated database to copy, so each test starts from a fresh schema in milliseconds."""
    url = create_database(postgres, "migrated_template")
    engine = create_engine(url)
    migrate(engine)
    engine.dispose()  # a template cannot be copied while anyone is connected to it
    return "migrated_template"


@pytest.fixture
def empty_database(postgres: str) -> str:
    """The URL of a new database that has never been migrated."""
    return create_database(postgres, f"empty_{next(_databases)}")


@pytest.fixture
def database(postgres: str, migrated_template: str) -> str:
    """The URL of a new database with every migration applied."""
    return create_database(postgres, f"test_{next(_databases)}", template=migrated_template)


@dataclass(frozen=True)
class Keycloak:
    # Where to reach the realm; also the issuer its tokens name.
    realm_url: str
    # The demo users' generated sign-in values, by role.
    sign_ins: dict[str, str]


def keycloak_container() -> "DockerContainer":
    """The Keycloak container compose runs: the same image, realm and start script."""
    from testcontainers.core.container import DockerContainer

    return (
        DockerContainer(KEYCLOAK_IMAGE)
        .with_kwargs(entrypoint=["bash", "/opt/txrisk/start-dev.sh"])
        .with_volume_mapping(REALM_FOLDER, "/opt/keycloak/data/import", "ro")
        .with_volume_mapping(KEYCLOAK_START, "/opt/txrisk/start-dev.sh", "ro")
    )


@pytest.fixture(scope="session")
def keycloak() -> Iterator[Keycloak]:
    """Keycloak in Docker, importing the committed realm with freshly generated sign-in values
    for its demo users, exactly as compose does. It takes about half a minute to start."""
    _require_docker()
    sign_ins = {role: secrets.token_urlsafe(16) for role in ("analyst", "supervisor", "admin")}
    container = keycloak_container().with_exposed_ports(8080)
    for role, value in sign_ins.items():
        container.with_env(demo_signin_env(role), value)
    with container:
        host = container.get_container_host_ip()
        realm_url = f"http://{host}:{container.get_exposed_port(8080)}/realms/txrisk"
        _wait_for_realm(realm_url, timeout=240)
        yield Keycloak(realm_url=realm_url, sign_ins=sign_ins)


def _wait_for_realm(realm_url: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(f"{realm_url}/.well-known/openid-configuration") as r:
                json.load(r)
                return
        except (urllib.error.URLError, OSError, ValueError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest.fixture(scope="session")
def local_idp() -> Iterator[LocalIdentityProvider]:
    """A stand-in for Keycloak that signs whatever token a test needs."""
    with serve_identity_provider() as provider:
        yield provider
