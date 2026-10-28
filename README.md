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
# first fill in .env (from .env.example) and keycloak.env (from keycloak.env.example)
make up                # Postgres, Keycloak on 127.0.0.1:8080, the migration job, the API on :8000
make seed              # loads the parties, and days 0-90 of data/generated, as history (no labels)
curl localhost:8000/v1/ready
```

- `POST /v1/score` scores a Transaction and stores it with its Risk assessment. It is idempotent on `transaction_id`: a repeat returns the stored assessment (with an `Idempotent-Replayed: true` header) and never raises a second Alert. Only the High Risk band raises an Alert.
- `GET /v1/transactions/{id}`, `GET /v1/alerts`, `GET /v1/models/active`, `GET /v1/health` (liveness) and `GET /v1/ready` (database reachable and migrated).
- `POST /v1/api-keys`, `GET /v1/api-keys`, `POST /v1/api-keys/{key_id}/rotate` and `DELETE /v1/api-keys/{key_id}` manage API keys.
- The OpenAPI spec is at `/v1/openapi.json`, and interactive docs at `/v1/docs`.

Each Transaction is scored with the clock at its own `occurred_at` (ADR 0005), against the history stored strictly before it, so the API gives exactly the offline scores. Set `TXRISK_MODEL_BUNDLE=/models/<version>` in `.env` to score with a bundle from `make train` (`./models` is mounted at `/models`); without one the rules alone score. Schema changes are plain SQL files in `src/txrisk/store/migrations/`, applied only by `txrisk migrate`, which compose runs as its own job before the API starts. Ground truth, Fraud discoveries and Benign anomalies are never loaded into Postgres (ADR 0007).

`make backup` takes a Postgres backup into the compose `backups` volume (a one-off job for a scheduler or host cron, pruned to the retention count in `config/backup.toml`); CI restores one into a fresh database and checks it against its source. Restore procedure: `docs/runbooks/restore.md`.

Outside Docker, `txrisk migrate`, `txrisk seed` and `txrisk serve` read the database from `TXRISK_DATABASE_URL`.

### Who may call what

People sign in through Keycloak (OIDC) and call with its access token, `Authorization: Bearer ...`. Services call with a scoped API key, `X-API-Key: ...`. Every `/v1` endpoint needs one or the other; only liveness, readiness, the OpenAPI spec and the docs are open.

| Endpoint | Analyst | Supervisor | Admin | API key scope |
|---|---|---|---|---|
| `POST /v1/score` | | | | `score:write` |
| `GET /v1/transactions/{id}` | yes | yes | | `transactions:read` |
| `GET /v1/alerts` | yes | yes | | `alerts:read` |
| `GET /v1/models/active` | yes | yes | yes | `models:read` |
| `/v1/api-keys` (issue, list, rotate, revoke) | | | yes | none: a key can never manage keys |
| `/v1/health`, `/v1/ready`, `/v1/openapi.json`, `/v1/docs` | open | open | open | open |

Scoring is for services only. The roles are Keycloak realm roles (`analyst`, `supervisor`, `admin`); a person with several holds the permissions of each.

**Keycloak (development only).** Compose runs Keycloak on http://localhost:8080, published on this machine only, and imports the `txrisk` realm from [config/keycloak/txrisk-realm.json](config/keycloak/txrisk-realm.json): the three roles, a `txrisk-workbench` client for browser sign-in (PKCE), a `txrisk-demo-cli` client for signing in from the command line, and one demo user per role (`demo-analyst`, `demo-supervisor`, `demo-admin`). The demo users and `txrisk-demo-cli` exist for development and demos; a production realm has neither. The committed realm holds no credentials: each demo user's sign-in value is a `${TXRISK_DEMO_SIGNIN_<ROLE>}` placeholder that Keycloak fills in when it imports the realm. Keycloak reads them from its own `keycloak.env` (copy `keycloak.env.example`; the API never sees it), and [deploy/keycloak/start-dev.sh](deploy/keycloak/start-dev.sh) refuses to start Keycloak unless each is set to a value of at least 12 characters, because an unset placeholder would otherwise become a publicly known credential. `keycloak.env` can also set a bootstrap admin for the console. The dev-mode Keycloak keeps nothing between runs, so a changed value takes effect on the next start.

The API accepts tokens only from the realm clients in `TXRISK_OIDC_ALLOWED_CLIENTS` (the token's `azp`). It defaults to `txrisk-workbench` alone, which is what production uses; compose adds `txrisk-demo-cli`. A refused token gets a bare `invalid bearer token`, and the reason is logged on the server.

```bash
# a person: the demo Analyst, signed in through Keycloak
BEARER=$(set -a; . ./keycloak.env; uv run txrisk demo-sign-in analyst --realm-url http://localhost:8080/realms/txrisk)
curl -H "Authorization: Bearer $BEARER" localhost:8000/v1/alerts
# a service: the first API key comes from the operator command; after that Admins issue them over /v1
ISSUED=$(docker compose exec -T api txrisk api-key --name ledger --scope score:write)
curl -H "X-API-Key: $ISSUED" -H 'Content-Type: application/json' -d @transaction.json localhost:8000/v1/score
```

**API keys** read `txrisk_<key_id>_<random part>` and are shown once, when issued. Only a SHA-256 digest is stored, so a database leak does not leak them. Each carries scopes and a rate limit (600 requests a minute unless issued with another). To rotate one without downtime, an Admin calls `POST /v1/api-keys/{key_id}/rotate`: it returns a new key with the same name, scopes, limit and expiry (rotation replaces a key, it does not extend its life), and the old key keeps working for the overlap (`{"overlap_minutes": ...}`, a day by default, `TXRISK_API_KEY_ROTATION_OVERLAP_MINUTES`). Deploy the new key to the service within the overlap; after it the old key is refused. A key is rotated once (a second rotation is refused with 409), so there is only ever one live successor; next time, rotate the successor. After a leak, `DELETE /v1/api-keys/{key_id}` revokes a key at once.

**Hardening.** A key over its rate limit gets `429` with `Retry-After`; limits are counted per API process, so behind several replicas a key's effective limit is that many times its own. Bodies over `TXRISK_MAX_REQUEST_BYTES` (64 KiB) get `413`. Only the origins in `TXRISK_CORS_ORIGINS` may call from a browser (no wildcard, no cookies). Every response carries `nosniff`, `DENY` framing, `no-referrer`, HSTS, `no-store` and a `default-src 'none'` content security policy (relaxed on `/v1/docs` just enough to load Swagger UI). Without `TXRISK_OIDC_ISSUER` the API accepts API keys only.

## Event stream

Transactions mainly arrive as events on Redpanda (ADR 0004). `make up` also starts Redpanda (on `localhost:19092` from the host) and one scoring worker.

```bash
make seed              # history up to day 90, as for the API
make produce           # publishes days 90-120 of data/generated, in event-time order
docker compose up --detach --scale worker=3   # three workers share the partitions
```

| Topic | Event | Key |
| --- | --- | --- |
| `transaction.created` | A Transaction, as the ledger reports it | Subject wallet ID |
| `sim_swap.recorded` | A SIM swap on a Wallet, when it happens | Wallet ID |
| `score.produced` | The stored Risk assessment of a Transaction | transaction ID |
| `fraud.discovered` | A Fraud discovery (complaint, chargeback or Case); no producer yet | transaction ID |

The versioned JSON Schemas in [`schemas/`](schemas) are the documented ledger contract. They are written from the Pydantic models that validate every event at both ends (`make schemas`), and a test fails if the two disagree.

- **Exactly once by transaction ID.** Workers score through the same idempotent store as `POST /v1/score`, and commit a batch's offsets only after its assessments are stored and its `score.produced` events are confirmed. A redelivered or replayed event gets the stored assessment back and republishes it unchanged, so `score.produced` may repeat a result, never change it. Only the High Risk band raises an Alert, once.
- **The same scores as the API.** Both paths score each Transaction with the clock at its `occurred_at` against the history stored before it, so they give identical assessments.
- **SIM swaps** after the seeded history arrive on `sim_swap.recorded`. Before scoring a batch, a worker stores every SIM swap on that topic, and the producer has each SIM swap confirmed before it publishes any later Transaction, so a Transaction's features always see the SIM swaps before it.
- **Ordering.** Transactions are keyed by Subject wallet, so each Subject wallet's own history is scored in order. Histories across Subject wallets (Transfers into a Wallet, an Agent's Cash-outs) span partitions, so with several workers they are as complete as the other partitions have got.
- **Recent-history cache.** With `TXRISK_REDIS_URL` set (compose sets it for the API, workers and seed), the services keep each Wallet's and Agent's last 30 days of Transactions in Redis sorted sets, trimmed by event time, and the features read history from there (ADR 0006). The cache never changes a score: each set carries a watermark of how far back it is complete, and any read it cannot fully answer, such as a Wallet's whole history once it is older than 30 days, falls back to Postgres. A CI parity test checks the cached features equal the offline replay's, and that flushing Redis mid-stream changes no score. Without `TXRISK_REDIS_URL`, history is read from Postgres alone. The compose Redis has no password because it is reachable only on the compose network; use an authenticated Redis anywhere else. Every process that stores Transactions must use the same Redis (`txrisk seed` flushes it); the rules are in `src/txrisk/store/redis_history.py`.
- **Rejected events** (invalid against the contract, or naming an unregistered party) go to `<topic>.dlq` with the reason in an `error` header. Any other failure stops the worker without committing, and its batch is redelivered.

Outside Docker, `txrisk worker` reads `TXRISK_DATABASE_URL` and `TXRISK_KAFKA_BOOTSTRAP_SERVERS` (and optionally `TXRISK_MODEL_BUNDLE`, `TXRISK_TOPIC_PARTITIONS`, `TXRISK_CONSUMER_GROUP`), and `txrisk produce` takes `--bootstrap-servers`.

## CI/CD

`.github/workflows/ci.yml` runs these jobs in parallel on every PR and push to main (uv's cache is on):

- `check`: ruff, `ruff format --check`, strict mypy and pytest with coverage. Coverage is gated at 80% on the **core logic only** (features, rules and scoring, the store's scoring and records, and the evaluation thresholds), as `[tool.coverage.report]` in `pyproject.toml` lists. Check it locally with `uv run pytest --cov`.
- `smoke`: the training smoke test, `generate -> features -> train -> evaluate` through the CLI on a tiny market (`uv run pytest -m smoke`).
- `audit`: `pip-audit` over the locked dependencies. It has no severity filter, so any known vulnerability fails the build.
- `trivy`: Trivy scans the built image, and the Dockerfile and compose file, and fails on high and critical findings. The image scan passes `--ignore-unfixed`: it skips vulnerabilities for which Debian has published no fixed version yet, because rebuilding cannot remove them (on 2026-10-28: 47 High findings in the Debian 13.7 base's util-linux, ncurses, systemd, acl and perl packages, all without a fix; the Python dependencies had none). The image build runs `apt-get upgrade`, so the gate fails as soon as a fix exists. There is no `.trivyignore` and the severity threshold is not lowered; the Dockerfile and compose scan has no exception, and neither has `pip-audit`.
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
