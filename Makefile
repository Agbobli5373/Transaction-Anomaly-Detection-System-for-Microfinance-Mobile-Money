.PHONY: install lint format typecheck test check generate features evaluate

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
