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
