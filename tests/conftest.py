import itertools
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from txrisk.generator import GeneratorConfig, load_config
from txrisk.store import create_engine, migrate

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "generator.toml"
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
    with PostgresContainer("postgres:16-alpine", driver="psycopg") as container:
        yield container.get_connection_url()


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
