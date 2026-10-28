.PHONY: install lint format typecheck test check generate features evaluate evaluate-supervised evaluate-stacked train paysim up down seed produce replay scheduler purge schemas backup openapi

install:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:
	uv run pytest

check: lint typecheck test

# Reads TXRISK_PSEUDONYM_KEY from the environment (see .env.example).
generate:
	uv run txrisk generate --config config/generator.toml --seed 42 --out data/generated

features:
	uv run txrisk features --data data/generated --out data/features/features.parquet

# Scores the evaluation days with the rules and writes reports/rules-only/.
evaluate:
	uv run txrisk evaluate --data data/generated --out reports/rules-only

# Trains the supervised model on days 0-60, bands it at the Alert budget on days 60-90 and judges
# its fraud probability alone on the evaluation days; writes reports/supervised/.
evaluate-supervised:
	uv run txrisk evaluate --data data/generated --scorer supervised --out reports/supervised

# Trains the model bundle as `train` does and judges its stacked Risk score on the evaluation days;
# writes reports/stacked/.
evaluate-stacked:
	uv run txrisk evaluate --data data/generated --scorer stacked --out reports/stacked

# Trains a model bundle into models/<version>/ and records the run in the local MLflow store
# (mlflow.db, mlruns/). Browse it with: uv run mlflow ui --backend-store-uri sqlite:///mlflow.db
train:
	uv run txrisk train --data data/generated --features data/features/features.parquet

# Judges every scorer on a local PaySim CSV (Kaggle: ealaxi/paysim1) as an external benchmark;
# writes reports/paysim/<scorer>/. Usage: make paysim PAYSIM_CSV=path/to/PS_..._log.csv
paysim:
	uv run txrisk paysim --csv $(PAYSIM_CSV) --features data/features/paysim.parquet

# The serving stack in Docker: Postgres, Keycloak on 127.0.0.1:8080, Redpanda, the migration job,
# then the API on :8000 and a scoring worker. Reads its settings and credentials from .env and
# Keycloak's from keycloak.env (see .env.example and keycloak.env.example); Keycloak refuses to
# start without its demo sign-ins.
up:
	docker compose up --build --detach --wait api worker keycloak

down:
	docker compose down

# Loads the parties and days 0-90 of data/generated into the compose Postgres as history.
seed:
	docker compose run --rm seed

# Publishes days 90-120 of data/generated to transaction.created and sim_swap.recorded in
# event-time order, standing in for the ledger; the workers score them (after `make seed`).
produce:
	docker compose run --rm produce

# Replays the live days of data/generated, Transactions, SIM swaps and Fraud discoveries, in
# event-time order at a day a minute. Faster: make replay SECONDS_PER_DAY=5 (0 is as fast as possible).
SECONDS_PER_DAY ?= 60
replay:
	docker compose run --rm replay txrisk replay --data /data/generated --seconds-per-day $(SECONDS_PER_DAY)

# Runs the scheduler against the compose Postgres: it fires the jobs in config/scheduler.toml, as
# one-off compose containers, once per simulated period as the replay advances the clock.
scheduler:
	set -a && . ./.env && set +a && TXRISK_DATABASE_URL=postgresql://txrisk_app@localhost:5432/txrisk uv run txrisk scheduler

# Deletes data older than config/retention.toml allows, measured on the simulation clock.
purge:
	docker compose run --rm job txrisk purge

# Rewrites the event topics' JSON Schemas in schemas/ from their models.
schemas:
	uv run txrisk schemas

# Regenerates the committed OpenAPI snapshot CI checks for breaking changes.
openapi:
	uv run python -m txrisk.contract_check openapi > contracts/openapi.json

# One Postgres backup into the compose `backups` volume, pruned to config/backup.toml's count.
# Restoring one: docs/runbooks/restore.md
backup:
	docker compose run --rm backup
