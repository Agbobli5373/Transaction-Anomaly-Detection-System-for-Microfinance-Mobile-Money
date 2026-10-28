# Transaction Risk Scoring for Mobile Money

Scores Ghana-style mobile money Transactions for risk and explains why, so people can review unusual activity. The system flags; it never blocks a Transaction or declares fraud. See [GLOSSARY.md](GLOSSARY.md) for the vocabulary and [docs/adr](docs/adr) for key decisions.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # then set TXRISK_PSEUDONYM_KEY to a long random value
make generate          # writes the synthetic dataset to data/generated/
make features          # writes point-in-time features to data/features/
make check             # lint, type-check and test
```

## Scoring API

The sync scoring API runs on Postgres, in Docker. The tests start their own Postgres with Testcontainers, so `make check` needs Docker running.

```bash
# first fill in the Postgres variables in .env (see .env.example)
make up                # Postgres, then the migration job, then the API on http://localhost:8000
make seed              # loads the parties, and days 0-90 of data/generated, as history (no labels)
curl localhost:8000/v1/ready
```

- `POST /v1/score` scores a Transaction and stores it with its Risk assessment. It is idempotent on `transaction_id`: a repeat returns the stored assessment (with an `Idempotent-Replayed: true` header) and never raises a second Alert. Only the High Risk band raises an Alert.
- `GET /v1/transactions/{id}`, `GET /v1/alerts`, `GET /v1/models/active`, `GET /v1/health` (liveness) and `GET /v1/ready` (database reachable and migrated).
- The OpenAPI spec is at `/v1/openapi.json`, and interactive docs at `/v1/docs`.

Each Transaction is scored with the clock at its own `occurred_at` (ADR 0005), against the history stored strictly before it, so the API gives exactly the offline scores. Set `TXRISK_MODEL_BUNDLE=/models/<version>` in `.env` to score with a bundle from `make train` (`./models` is mounted at `/models`); without one the rules alone score. Schema changes are plain SQL files in `src/txrisk/store/migrations/`, applied only by `txrisk migrate`, which compose runs as its own job before the API starts. Ground truth, Fraud discoveries and Benign anomalies are never loaded into Postgres (ADR 0007).

`make backup` takes a Postgres backup into the compose `backups` volume (a one-off job for a scheduler or host cron, pruned to the retention count in `config/backup.toml`); CI restores one into a fresh database and checks it against its source. Restore procedure: `docs/runbooks/restore.md`.

Outside Docker, `txrisk migrate`, `txrisk seed` and `txrisk serve` read the database from `TXRISK_DATABASE_URL`.

## CI/CD

`.github/workflows/ci.yml` runs these jobs in parallel on every PR and push to main (uv's cache is on):

- `check`: ruff, `ruff format --check`, strict mypy and pytest with coverage. Coverage is gated at 80% on the **core logic only** (features, rules and scoring, the store's scoring and records, and the evaluation thresholds), as `[tool.coverage.report]` in `pyproject.toml` lists. Check it locally with `uv run pytest --cov`.
- `smoke`: the training smoke test, `generate -> features -> train -> evaluate` through the CLI on a tiny market (`uv run pytest -m smoke`).
- `audit`: `pip-audit` over the locked dependencies. It has no severity filter, so any known vulnerability fails the build.
- `trivy`: Trivy scans the built image, and the Dockerfile and compose file, and fails on high and critical findings.
- `contracts`: a breaking change to the OpenAPI spec (`oasdiff breaking`) or to any `schemas/*.json` event schema, compared with the base commit, fails the build. `contracts/openapi.json` is the committed snapshot of `/v1/openapi.json`; after an API change, regenerate it with `make openapi`. A test fails while the snapshot is stale.
- `compose`: the compose stack starts and reports ready.
- `restore`: a backup restores into a fresh database and matches its source.
- `publish`: on pushes to main, once the jobs above pass, pushes `ghcr.io/<owner>/<repo>:<commit sha>` using `GITHUB_TOKEN` (`packages: write`).

### Branch protection (repo owner)

Branch protection cannot be set from CI or by the agents that built it. In the repository, go to Settings > Branches > Add branch ruleset (or classic rule) for `main` and turn on:

- Require a pull request before merging.
- Require status checks to pass before merging, and require branches to be up to date. Add the checks `check`, `smoke`, `audit`, `trivy`, `contracts`, `compose` and `restore`.
- Block force pushes and deletion of `main`.

`publish` runs only on main, so it is not a required check.
