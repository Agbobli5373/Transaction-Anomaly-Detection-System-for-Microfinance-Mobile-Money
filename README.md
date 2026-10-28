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
make seed              # loads the parties, and days 0-90 of data/generated, as history, with the Fraud labels discovered before day 90
curl localhost:8000/v1/ready
```

- `POST /v1/score` scores a Transaction and stores it with its Risk assessment. It is idempotent on `transaction_id`: a repeat returns the stored assessment (with an `Idempotent-Replayed: true` header) and never raises a second Alert. Only the High Risk band raises an Alert.
- `GET /v1/transactions/{id}`, `GET /v1/alerts`, `GET /v1/models/active`, `GET /v1/health` (liveness) and `GET /v1/ready` (database reachable and migrated; also whether scoring is degraded).
- `POST /v1/api-keys`, `GET /v1/api-keys`, `POST /v1/api-keys/{key_id}/rotate` and `DELETE /v1/api-keys/{key_id}` manage API keys.
- `GET /v1/audit` reads the audit log (Admins only; see below).
- The OpenAPI spec is at `/v1/openapi.json`, and interactive docs at `/v1/docs`.

Each Transaction is scored with the clock at its own `occurred_at` (ADR 0005), against the history stored strictly before it, so the API gives exactly the offline scores. Set `TXRISK_MODEL_BUNDLE=/models/<version>` in `.env` to score with a bundle from `make train` (`./models` is mounted at `/models`); without one the rules alone score. Schema changes are plain SQL files in `src/txrisk/store/migrations/`, applied only by `txrisk migrate`, which compose runs as its own job before the API starts. Ground truth and Benign anomalies are never loaded into Postgres. `seed` loads the Fraud discoveries made before its cutoff as Fraud labels, each dated by its own discovery time, which is what the system would hold had it been receiving `fraud.discovered` events all along; later ones arrive only through `fraud.discovered` events (ADR 0007). Without them the first retraining would see no Fraud at all.

### Busy days

The High threshold is not fixed. On a festive day the whole market spends more, every amount-driven score rises with it, and a fixed threshold would raise twice the Alert budget (#47). Each assessment therefore reads the **market spend index** (the market's spend in the last 6 and 24 complete hours against the same hours on each of the previous 7 days, a feature in the shared feature function) and the bundle's High threshold rises with it, as if the Alert budget were `index ^ surge_elasticity` times smaller (`config/evaluation.toml`; ADR 0011). Medium does not move, so a Transaction that stops being High lands on the Medium watch list, and a Critical rule hit is High on any day. Postgres keeps the market's spend per hour in `market_hours`, maintained by a trigger on `transactions` (migration 0010).

### Degraded mode

If the Champion model bundle (`TXRISK_MODEL_BUNDLE`) is missing, corrupt or fails to load, or scoring with it fails or takes longer than `TXRISK_SCORING_TIMEOUT_MS` (default 5000: the first call after a load is cold and slow), the API and the workers keep scoring from Rule hits alone (ADR 0009). The assessment has `degraded: true` and `model_version: "rules-only"`, and its `risk_score` and `risk_band` are on the **rules scale** (whole numbers, High from 50, a Critical rule at least 90), not the model's (a probability x 100, High near the bundle's threshold). Critical rules still force High. `GET /v1/ready` stays 200 and reports `degraded`, `reason` and `awaiting_rescore`.

Once the model is healthy again the degraded Transactions are re-scored and their stored assessments replaced (the old ones are kept in `risk_assessment_history`). An Alert already raised is kept and never duplicated; a Transaction that re-scores to High raises its Alert once. The API and workers re-score after recovering; `txrisk rescore [--limit N]` does it on demand (exit code 3 while the model is still down), for the scheduler. After a failure the model is skipped for `TXRISK_MODEL_RETRY_SECONDS` (default 30) before it is tried again.

Counters (`txrisk_degraded_assessments_total{reason}`, `txrisk_rescored_total`, `txrisk_model_load_failures_total`) go through the small `txrisk.scoring.Metrics` interface, held in process for now; the observability work wires it to Prometheus.

`make backup` takes a Postgres backup into the compose `backups` volume (a one-off job for a scheduler or host cron, pruned to the retention count in `config/backup.toml`); CI restores one into a fresh database and checks it against its source. Restore procedure: `docs/runbooks/restore.md`.

Outside Docker, `txrisk migrate`, `txrisk seed` and `txrisk serve` read the database from `TXRISK_DATABASE_URL`. `txrisk migrate` also creates the `txrisk_app` database role (see Audit log below) when `TXRISK_APP_DB_PASSWORD` is set; the API and workers connect as that role, the migration job, `seed` and `backup` as the owner. After restoring a backup, run `txrisk migrate` again to restore the role's grants (a restore skips them, so it works on a cluster without the role).

### Who may call what

People sign in through Keycloak (OIDC) and call with its access token, `Authorization: Bearer ...`. Services call with a scoped API key, `X-API-Key: ...`. Every `/v1` endpoint needs one or the other; only liveness, readiness, the OpenAPI spec and the docs are open.

| Endpoint | Analyst | Supervisor | Admin | API key scope |
|---|---|---|---|---|
| `POST /v1/score` | | | | `score:write` |
| `GET /v1/transactions/{id}` | yes | yes | | `transactions:read` |
| `GET /v1/alerts` | yes | yes | | `alerts:read` |
| `GET /v1/cases`, `GET /v1/cases/{id}` | yes: their own and the unassigned open ones | yes: all | | none |
| `POST /v1/cases/{id}/notes`, `POST /v1/cases/{id}/transition` | yes: only on a Case assigned to them | yes | | none |
| `POST /v1/cases/{id}/assign` | | yes | | none |
| `POST /v1/cases/{id}/report` (draft) | yes: only on a Case assigned to them | yes | | none |
| `POST /v1/cases/{id}/report/approve`, `GET /v1/cases/{id}/report/export` | | yes | | none |
| `GET /v1/watchlist` | yes | yes | | none |
| `GET /v1/models/active` | yes | yes | yes | `models:read` |
| `/v1/api-keys` (issue, list, rotate, revoke) | | | yes | none: a key can never manage keys |
| `GET /v1/audit` | | | yes | none |
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

### Cases

An Alert joins its Subject wallet's active Case (open or investigating), or opens a Case if the wallet has none; a unique partial index allows one active Case per wallet, so replays and concurrent Alerts never make two, and the Case is assigned in the same transaction that raises the Alert. A Case runs `open -> investigating -> closed_fraud | closed_legitimate`; any other move is a `409` (`422` for a status that is not a target at all), and a closed Case is never reopened, assigned or noted. A Supervisor assigns a Case to an Analyst (by Keycloak subject ID); only the assigned Analyst or a Supervisor adds notes or moves it. An Alert's disposition is not stored: `confirmed_fraud` or `dismissed` once its Case closes as fraud or legitimate, else null.

`GET /v1/cases` filters by `status`, `assignee` (a subject ID, or `me`) and `wallet`, and pages by `before`/`next_before`; an Analyst sees the Cases assigned to them and the unassigned open ones, a Supervisor all. `GET /v1/cases/{id}` returns the Alerts with Risk score, Reasons and disposition, the Case's notes and events, and the Subject wallet's 20 latest Transactions. `GET /v1/watchlist` lists Medium-band Transactions (no Alerts), filtered by `wallet`, `region` and `transaction_type`.

A **Suspicious transaction report** is drafted (`POST /v1/cases/{id}/report`, with a `summary`) only from a Case closed as fraud, by the Analyst assigned to it or a Supervisor; drafting again rewrites the draft. Only a Supervisor approves it (`POST .../report/approve`; an Analyst, an Admin or an API key gets 403), and approval is final (a database trigger refuses any later change). Only an approved report exports (`GET .../report/export`, Supervisors only, 409 for a draft): a JSON file download holding the Case, the Transactions Alerted on it with their Risk assessments and Reasons, its notes and move comments, and the summary. Wallets appear only by pseudonymised ID, and anything shaped like a phone number in free text (notes, summary) is masked to its last three digits. Drafting, approving and exporting are audited as `report.draft`, `report.approve` and `report.export` (denied and failed attempts too, never the summary's text). The system produces the file and never files it. The report is in `GET /v1/cases/{id}` as `report`, and the workbench's Case view drafts, approves and exports it.

Closing as fraud is a Fraud discovery for each Transaction alerted on the Case, at the closing event time (the simulation clock, never the wall clock): in the closing transaction the Fraud labels are stored (source `case`) and one `fraud.discovered` event per Transaction is written to the `outbox` table. `txrisk outbox` (the compose `outbox` service) publishes the outbox to Redpanda with the idempotent producer and marks rows published only once the broker confirms them, so a crash between commit and publish loses nothing; delivery is at least once and the label store keeps the first label. A closed-fraud Case keeps who closed it and when, its Alerts, notes and events, which a Suspicious transaction report is built from.

### Audit log

Every sensitive action appends an entry to the append-only `audit_log` table: who (a person's Keycloak subject and roles, or a service's API key ID, or the operator command), what (`transaction.view`, `alert.list`, `api_key.issue`, `api_key.list`, `api_key.rotate`, `api_key.revoke`, `audit.read`, `case.list`, `case.view`, `case.assign`, `case.note`, `case.transition`, `watchlist.list`), which object (type and ID), when, and the outcome: `success`, `denied` (a 403 on an audited route) or `failed` (permitted, but no such object or a conflict). The time is the wall clock at the request, because an API request has no event time (ADR 0005 keeps scoring off the wall clock). An entry holds identifiers and small facts only (an issued key's name, scopes and ID), never a token, an API key or a raw MSISDN; an object ID a caller typed that is not a known identifier, or that looks like a phone number, is stored as `[redacted]`.

- **Not audited, by design:** `POST /v1/score` (a high-volume service call), health and readiness, `GET /v1/models/active`, and 401s (there is no caller to name, and an unauthenticated flood would only fill the log). Later features (Cases, model Promotion, report export) record through the same helper, `txrisk.api.audit` (`require(permission, AuditedRoute(...))` for refusals, `Auditor.record(...)` inside the action's own transaction), so an action that cannot be audited does not happen.
- **Append-only to the application.** The API and workers connect as `txrisk_app`, which can only SELECT and INSERT on `audit_log`, owns no table (so it cannot alter or drop one or its triggers), and cannot create objects; its grants for every table are listed in `txrisk.store.roles.GRANTS`, and a new table without a decision there stops `txrisk migrate`. Triggers on `audit_log` also refuse UPDATE, DELETE and TRUNCATE for everyone until the owner deliberately disables them. The owner role, which runs migrations, is for the migration job and operators, never the API.
- **Retention: the one way an entry leaves.** `txrisk purge` calls the database function `audit_log_purge(retention)` (migration 0004), which only the owner can execute: it deletes entries older than the configured period (`audit_log` in `config/retention.toml`, days of wall-clock time) and refuses a period under 365 days. The DELETE trigger lets a row go only inside that function (it sets a transaction-local flag) and only when the row is older than a one-year floor, so a forged flag still cannot remove a recent entry; no DELETE of any age works without the flag, and `txrisk_app` has neither DELETE nor EXECUTE. A superuser or the table's owner can still disable the triggers outright; that is a deliberate, visible act outside the application, not a path the application has.
- **Roles by job.** `txrisk_app`: the API, workers and the scheduler (SELECT/INSERT/UPDATE only where each table needs it: the simulation clock is UPDATE, `fraud_labels` INSERT, `scheduled_jobs` UPDATE). The owner: `migrate`, `seed`, `backup`, `purge` and anything the scheduler launches in the `job` service (retraining and monitoring), because those jobs are not the request path.
- **Reading it.** `GET /v1/audit` (Admins) filters by `actor` (subject or key ID), `actor_type`, `action`, `object_type`, `object_id`, `outcome` and a `since`/`until` range, newest first, and pages with `before` = the previous page's `next_before`. Reading the log is itself audited, with the filters used.
- **Known risk: an Admin can issue themselves a read key.** `api_key.issue` records who issued which key with which scopes, and every call the key then makes is recorded under its key ID, so the trail runs from the Admin to what the key read. Admins are the accountable role for this, so the log should be reviewed.
- **Credentials.** The role's password is `TXRISK_APP_DB_PASSWORD` in the gitignored `.env`, a different random value from the owner's; the migration job sends libpq's SCRAM hash of it, never the plain value, and the API reads it from the environment. Re-running `txrisk migrate` with a new value rotates it.

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
| `fraud.discovered` | A Fraud discovery (complaint, chargeback or Case); `make replay` publishes the generated ones | transaction ID |

The versioned JSON Schemas in [`schemas/`](schemas) are the documented ledger contract. They are written from the Pydantic models that validate every event at both ends (`make schemas`), and a test fails if the two disagree.

- **Exactly once by transaction ID.** Workers score through the same idempotent store as `POST /v1/score`, and commit a batch's offsets only after its assessments are stored and its `score.produced` events are confirmed. A redelivered or replayed event gets the stored assessment back and republishes it unchanged, so `score.produced` may repeat a result, never change it. Only the High Risk band raises an Alert, once.
- **The same scores as the API.** Both paths score each Transaction with the clock at its `occurred_at` against the history stored before it, so they give identical assessments.
- **SIM swaps** after the seeded history arrive on `sim_swap.recorded`. Before scoring a batch, a worker stores every SIM swap on that topic, and the producer has each SIM swap confirmed before it publishes any later Transaction, so a Transaction's features always see the SIM swaps before it.
- **Ordering.** Transactions are keyed by Subject wallet, so each Subject wallet's own history is scored in order. Histories across Subject wallets (Transfers into a Wallet, an Agent's Cash-outs) span partitions, so with several workers they are as complete as the other partitions have got.
- **Recent-history cache.** With `TXRISK_REDIS_URL` set (compose sets it for the API, workers and seed), the services keep each Wallet's and Agent's last 30 days of Transactions in Redis sorted sets, trimmed by event time, and the features read history from there (ADR 0006). The cache never changes a score: each set carries a watermark of how far back it is complete, and any read it cannot fully answer, such as a Wallet's whole history once it is older than 30 days, falls back to Postgres. A CI parity test checks the cached features equal the offline replay's, and that flushing Redis mid-stream changes no score. Without `TXRISK_REDIS_URL`, history is read from Postgres alone. The compose Redis has no password because it is reachable only on the compose network; use an authenticated Redis anywhere else. Every process that stores Transactions must use the same Redis (`txrisk seed` flushes it); the rules are in `src/txrisk/store/redis_history.py`.
- **Rejected events** (invalid against the contract, or naming an unregistered party) go to `<topic>.dlq` with the reason in an `error` header. Any other failure stops the worker without committing, and its batch is redelivered.

- **Fraud labels.** Workers consume `fraud.discovered` and store each discovery in `fraud_labels`, dated by its own event time. Readers ask for the labels `known_at` a moment (`store.FraudLabels`), so a complaint or chargeback is invisible before its event time, however early it was stored; the first discovery of a Transaction stands.

Outside Docker, `txrisk worker` reads `TXRISK_DATABASE_URL` and `TXRISK_KAFKA_BOOTSTRAP_SERVERS` (and optionally `TXRISK_MODEL_BUNDLE`, `TXRISK_TOPIC_PARTITIONS`, `TXRISK_CONSUMER_GROUP`), and `txrisk produce` takes `--bootstrap-servers`.

## Simulation clock, scheduler and replay

All time-based behaviour runs on event time (ADR 0005). The **simulation clock** (`store.SimulationClock`, one row in Postgres) is the latest event time any worker or the API has seen; it only moves forward and is the only source of "now" for business logic. A test (`tests/test_wall_clock.py`) scans `src/txrisk` and fails on any read of the wall clock (`datetime.now`, `time.time`, `date.today`, SQL `now()`, and the like) that is not in its allow-list of operational uses, each with its reason.

```bash
make replay                       # days 90-120 as Transactions, SIM swaps and Fraud discoveries, a day a minute
make replay SECONDS_PER_DAY=5     # faster; 0 publishes as fast as possible
make scheduler                    # fire the scheduled jobs as the clock advances (runs on the host)
make purge                        # one retention purge now
```

- **Replay** (`txrisk replay`, after `make seed`) publishes the live days in event-time order. `--seconds-per-day` (default 60) only paces the publishing: every event keeps its event time. Fraud discoveries come from `fraud_discoveries.parquet` and are published when the replay reaches their event time; Ground truth is never opened.
- **Scheduler** (`txrisk scheduler`, `config/scheduler.toml`) fires each named job once per simulated day or week as the clock crosses it, so a replayed week fires the weekly jobs within minutes. The last period fired is kept in Postgres (`scheduled_jobs`), so restarts and a second scheduler never fire a period twice; periods the clock jumped over are not made up. A job runs as a detached one-off compose container (`--launcher compose`, `docker compose run` of the `job` service) or as a Kubernetes Job (`--launcher kubernetes`, in a cluster, named for the job and period).
- **Retention** (`txrisk purge`, `config/retention.toml`) deletes rows older than each table's period, measured on the simulation clock, and the scheduler runs it daily. The audit log is kept longest (a validated rule, and at least 365 days), but by wall-clock age, because its entries are stamped with the wall clock; see Audit log. `purge` deletes, so it runs as the owner role (compose's `job` service, which the scheduler launches, uses the owner; `txrisk purge` refuses `txrisk_app`).

## Case workbench

`workbench/` is a Next.js (App Router, strict TypeScript) app where Analysts and Supervisors work Cases. It uses the txrisk API only (no database access), through a client typed from `contracts/openapi.json` (`npm run api-types` regenerates `src/lib/api-types.ts`; CI fails if the committed file differs from the spec).

```bash
# .env needs AUTH_SECRET (openssl rand -hex 32), and keycloak.env the demo sign-in values
make workbench                            # the API, Keycloak and the workbench, built and started
docker compose run --rm seed-cases --tag demo --count 5 --medium 40   # demo Cases and Medium Transactions
open http://localhost:3000                # sign in as demo-analyst, demo-supervisor or demo-admin
```

Outside Docker: `cd workbench && cp .env.example .env` (set `AUTH_SECRET`), then `npm ci && npm run dev`. `npm run lint`, `typecheck`, `test` (Vitest) and `build` are the checks; `npm run e2e` runs Playwright (see below).

- **Screens.** The Case queue (filters: status, assignee, Wallet; cursor pagination), the Case view (each Alert's Risk score, Risk band and Reasons with their stated values, the Subject wallet's 20 latest Transactions, notes and event history, and the controls below) and the Medium watch list (filters: Wallet, region, Transaction type; cursor pagination).
- **Roles.** The workbench only hides or disables what a role cannot do; the API enforces it and its 403 and 409 answers are shown as they come. An **Analyst** sees the Cases assigned to them and the unassigned open ones, and can add notes and move Cases assigned to them (on other Cases the controls are disabled, with the reason). A **Supervisor** sees every Case, assigns a Case to an Analyst and works any Case. An **Admin** has no Case screens (a page says so). Only the moves the status allows are offered: open to investigating, investigating to closed as fraud or legitimate; a closed Case never reopens. The API has no directory of Analysts, so a Supervisor assigns by the Analyst's ID (their Keycloak subject), which an Analyst sees in the page header after signing in.
- **Sign-in and tokens.** Sign-in is the OIDC authorization code flow with PKCE (S256) against the `txrisk-workbench` Keycloak client, implemented in `src/lib/oidc.ts` on `jose`. The tokens stay on the Next.js server: the browser holds only an `httpOnly`, `SameSite=Lax` cookie (`Secure` and `__Host-` prefixed when `WORKBENCH_PUBLIC_URL` is https) that is an encrypted session ID, sealed with `AUTH_SECRET` (set it in `.env`; at least 32 characters; the server refuses to start without it). The session itself lives in the server's memory, so a restart signs people out and one workbench replica is assumed (put `session-store.ts` on Redis to scale out). Pages and server actions call the API from the server with the person's access token; none reaches browser JavaScript, `localStorage` or the page (the Playwright test checks). An access token is refreshed shortly before it expires; if the refresh is refused or the session passes its 8-hour limit, the person is signed out here and at Keycloak's end-session endpoint (the id token goes in that redirect as `id_token_hint`, which is how OIDC names the session to end). Sign out is a POST, checked for same-origin.
- **Hardening.** `src/proxy.ts` sets a per-request-nonce `Content-Security-Policy` (`script-src 'self' 'nonce-...' 'strict-dynamic'`, no inline styles or eval, `frame-ancestors 'none'`, `form-action` for this site and Keycloak only), `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy: same-origin`, `Permissions-Policy`, cross-origin isolation headers and `Cache-Control: no-store`. Phone-shaped numbers in free text (notes, comments) are masked on screen; the API already carries pseudonymised Wallet IDs, and nothing sensitive is logged (only the reason a sign-in failed). The pages are keyboard-operable with labelled controls, AA-contrast colours and responsive tables; the Playwright tests run axe (WCAG 2 A and AA) on every screen and check a phone-width layout.
- **Dev-only Keycloak settings.** The `txrisk-workbench` client in `config/keycloak/txrisk-realm.json` is a public client with PKCE required, redirect URI and post-logout redirect `http://localhost:3000/*`, web origin `http://localhost:3000`, and no secret. Another origin means changing it there and `WORKBENCH_PUBLIC_URL`, and `TXRISK_CORS_ORIGINS` is not needed (the browser never calls the API).
- **Docker.** `workbench/Dockerfile` is multi-stage (`npm ci`, build, then Next.js's standalone output on `node:22-slim`), runs as the non-root `node` user and is scanned by Trivy in CI. Compose publishes it on 127.0.0.1:3000 and gives it `AUTH_SECRET` from `.env`; the browser reaches Keycloak as `localhost:8080` and the server reaches it as `keycloak:8080`.
- **End-to-end tests.** `workbench/e2e/` drives the real stack (API, Keycloak, Postgres) with Playwright: sign in through Keycloak as the demo Analyst, open a Case, add a note and close it; a Supervisor assigns a Case and the Analyst sees it; an Analyst has no assignment control and the API's 403 is shown when the disabled form is forced; the Medium watch list filters and pages; an Analyst drafts a Suspicious transaction report from a Case closed as fraud but cannot approve it (the forced request gets the API's 403), and a Supervisor approves and exports it. The tests need no backdoor: `txrisk seed-cases` (`src/txrisk/store/demo_cases.py`) runs as the database owner and creates Wallets, routine Transactions, a High assessment with Reasons, its Alert and the Case, through the same store functions scoring uses, so the Cases look like real ones. The demo users' sign-in values are read from the environment, never written in a test:

```bash
docker compose up --build --detach --wait api keycloak workbench
cd workbench && npm ci && npx playwright install chromium
(set -a; . ../keycloak.env; set +a; npx playwright test)   # PW_CHROMIUM_PATH=... uses an installed Chromium
```

The tests change Cases and expect a fresh database (CI always has one; locally `docker compose down --volumes` between runs). `E2E_SEED_COMMAND` replaces the compose seeding service where it cannot run (`uv run txrisk seed-cases`, with `TXRISK_DATABASE_URL` the owner's).

Where things run on this machine (all published on 127.0.0.1 only unless noted):

| What | Where |
| --- | --- |
| Case workbench | <http://localhost:3000> |
| Evaluation pages | <http://localhost:8501> |
| Keycloak (sign-in, admin console if configured) | <http://localhost:8080> |
| Scoring API (docs at `/v1/docs`; published on all interfaces) | <http://localhost:8000> |
| Grafana, Prometheus, Jaeger | <http://localhost:3001>, <http://localhost:9090>, <http://localhost:16686> |
| Postgres (published on all interfaces) | `localhost:5432` |

## Evaluation pages

`evaluation-pages/` is a Streamlit app where ML engineers browse model versions. It reads the txrisk API only: its image installs the locked `pages` dependency group alone (no txrisk code, database driver or model), and the compose service gets no database setting, just `TXRISK_API_URL` and one API key.

```bash
docker compose exec api txrisk api-key --name evaluation-pages --scope models:read   # shown once
# put it in .env as TXRISK_EVALUATION_API_KEY, then:
make evaluation-pages                     # http://localhost:8501, published on this machine only
uv run txrisk evaluate --scorer bundle --bundle models/<version> --data data/generated --features data/features/features.parquet
```

- **Pages.** *Model versions* lists the Champion and Challenger models, every version retraining made and the Promotion history. *Model version* shows one version: headline metrics (PR-AUC, ROC-AUC, precision, recall and false positive rate at High, precision at the Alert budget), the PR and ROC curves, recall at High by Fraud scenario and by region, KYC tier, Channel and Wallet age band, recall against Ground truth next to recall against discovered Fraud labels (and the share of Fraud never discovered), and the model card's own metrics by segment. *Compare versions* puts two versions side by side (the Champion and the Challenger by default) with their differences, overlaid curves and per-scenario and per-segment recall.
- **Where the numbers come from.** The model card's metrics are on the calibration days against the Fraud labels known when the version went live, and every version has them. Scenario recall and the Ground truth gap exist only offline, so `txrisk evaluate --scorer bundle --bundle models/<version>` judges a saved bundle on the evaluation days and saves its report in `models/<version>/evaluation/`. `GET /v1/models/{version}/evaluation` serves that file to any role or a key with `models:read` (404 without one), so Ground truth stays out of the API's own inputs (ADR 0007); the API only serves a finished report. The reports also hold the curves as at most 200 points. A version with no report is listed and the pages say how to make one. The Challengers retraining makes have no report until someone runs the command, and the evaluation days (91-120) are the days the live replay uses, so judge a retrained version on them with that in mind.
- **Access.** The key is read from `TXRISK_EVALUATION_API_KEY` in the gitignored `.env` (compose passes it to this container alone, in `environment`, never an `env_file`). Without it the pages show how to issue one; a key without `models:read` is refused by the API and the pages say so. The tests (`tests/test_evaluation_pages.py`) drive the pages with Streamlit's `AppTest` against a stand-in for the API, check that they only issue `GET /v1/models...` and that nothing in them can reach a database.

## Observability

`docker compose up` also starts Prometheus, Alertmanager, Grafana and Jaeger, all bound to this machine only. Set `GF_SECURITY_ADMIN_PASSWORD` in `.env` first (Grafana refuses to start without it).

| What | Where |
| --- | --- |
| Operations dashboard (Grafana) | <http://localhost:3001>, user `admin`, the password from `.env` |
| Prometheus | <http://localhost:9090> |
| Alertmanager | <http://localhost:9093> |
| Model health and Fraud outcomes dashboards | Grafana, as above |
| Traces (Jaeger) | <http://localhost:16686>, once `TXRISK_TRACING_ENABLED=true` is in `.env` |

- **Metrics.** The API (`TXRISK_METRICS_PORT`, 9101 in compose), the worker (9100), and `replay` and `produce` (9102) serve Prometheus metrics on a port of their own at `/metrics`. They are unauthenticated and carry only counts and timings, so compose never publishes them: Prometheus scrapes them over the compose network, and they are not under `/v1`. The scheduler serves them with `--metrics-port`. Run a replay with `docker compose run --rm --use-aliases replay` for Prometheus to find it. The series are `txrisk_scoring_duration_seconds` and `txrisk_http_request_duration_seconds` (latency histograms), `txrisk_scores_total{path,band,degraded}` (throughput, the Risk band mix and the Degraded mode rate), `txrisk_alerts_total{day}` (Alerts per simulated day), `txrisk_scoring_errors_total`, `txrisk_events_consumed_total`, `txrisk_consumer_lag{topic,partition}` (the worker reads each assigned partition's end offset less its committed offset) and `txrisk_events_published_total`. Degraded mode is counted from `RiskAssessment.degraded`, so whichever path produces a degraded assessment is counted.
- **Logs** are one JSON object per line on stderr. Every line inside a scoring request or an event carries `correlation_id` and `transaction_id` (plus `trace_id` when tracing). The correlation id is the request's `X-Correlation-ID` (echoed in the response) or the event's `correlation_id` header, else a new one; it is put on `score.produced` and dead-letter events as a header. Tokens, API keys and MSISDN-shaped numbers are masked in every message and exception text.
- **Traces** are off until `TXRISK_TRACING_ENABLED=true`: with it unset there is no exporter, no background thread and nothing in event headers. They go as OTLP/HTTP to `OTEL_EXPORTER_OTLP_ENDPOINT` (Jaeger, in compose). One Transaction is one trace: the API's request (or the producer's publish and the worker's consume) down through `score.transaction`, `store.lookup`, `features.compute`, `history.redis` (`cache.hit`) or `history.postgres.*`, `model.score`, `store.save` and `cache.record`. The `traceparent` event header carries the trace across Redpanda.
- **Dashboard.** `deploy/grafana/dashboards/operations.json` is provisioned read-only with the datasources in `deploy/grafana/provisioning/`: latency percentiles, throughput, errors, consumer lag, Degraded mode rate, Alerts per day and the Risk band mix.

## Model monitoring

`txrisk monitor` (`txrisk.monitoring`, `config/monitoring.toml`) is the daily health check of the Champion model, on the simulation clock. The scheduler fires it once per simulated day (`[jobs.model-monitor]` in `config/scheduler.toml`, run in the compose `job` service as the owner; `make monitor` runs it by hand) for the day just ended. It stores what it computed in `monitoring_metrics` and pushes it to the Pushgateway, so the whole path is: job, Postgres and Pushgateway, Prometheus, Alertmanager, the alert sink, Grafana.

- **Drift.** PSI of each model input and of the Risk score for the day against the **Training baseline** inside the bundle (`txrisk train` stores each column's quantile bins and shares, plus a share for missing values: a few KB, from the threshold days only, never later ones; the bundle `version` changes with it). 0.1 warns and 0.25 needs action, both in `config/monitoring.toml`. A bundle trained before baselines still loads; monitoring then reports *no baseline* instead of measuring. Inputs that only grow as the data ages (`history_count`, `device_age_seconds`) are listed in `psi.ignore`: their PSI is large by construction. Fraud is about 0.1% of Transactions, so a change in how it looks hardly moves the whole population's PSI; the job therefore also measures PSI **among the Alerts** (the inputs of High Transactions over the last seven days against the High Transactions in the baseline), where such a change concentrates.
- **Alert volume** against the Alert budget (`alert_budget_per_day`) and a band around it, the **Risk band mix**, and the **share of Fraud discoveries** by source (complaint, chargeback, Case) and Channel.
- **Label-based performance** (precision, recall, false positive rate at High, overall and by region, KYC tier, Channel and Wallet age band) only over Transactions older than the Label maturity window and only against the Fraud labels visible at the simulated time (ADR 0007: Ground truth is never read). **Recall against discovered labels** (the share of scored, Fraud-labelled Transactions that were High) needs no maturity.
- **Metrics reach Prometheus through a Pushgateway**, not an exporter: the job is a one-off container that exits in seconds, so there is nothing to scrape, and an exporter serving the stored values would be another service with database access. The Pushgateway is on the compose network only; each run replaces the whole group, so a segment that is gone leaves no stale series. The job also pushes the thresholds it judged by, so the alert rules (`deploy/prometheus/rules/monitoring.yml`) carry no limit of their own.
- **Alerts.** Prometheus rules fire at the action thresholds (PSI of any input, of the Risk score or among the Alerts; Alert volume outside the budget band; precision or recall falling by more than `labels.max_relative_drop` against their mean over the previous days) into Alertmanager (<http://localhost:9093>), which sends them to the **alert sink** (`txrisk alert-sink`), a webhook that records what it receives and exposes it as Prometheus metrics. A warning is also sent. Replace the receiver in `deploy/alertmanager/alertmanager.yml` with a pager or chat integration to be told for real.
- **Dashboards** (Grafana, provisioned): **Model health** (PSI per input, for the Risk score and among the Alerts, the band mix, Alert volume against the budget, label-based metrics by segment, firing alerts and the alerts the sink received) and **Fraud outcomes** (discoveries by Channel, complaint against Case share, recall against discovered labels over time). They read Prometheus only, so Grafana holds no database credential.
- **Retention.** `model_inputs` (the inputs each Transaction was scored with, which PSI needs) is kept 60 days (the backtest gate scores the matured window from them too) and `monitoring_metrics` 365, both in `config/retention.toml`.

## Retraining and the Challenger model

`txrisk retrain` (`txrisk.retraining`, `config/retraining.toml`) trains a new bundle from what Postgres holds at the simulated time and registers it as the **Challenger model**. The scheduler fires it weekly (`[jobs.retrain-weekly]`), and a second daily job (`[jobs.retrain-on-drift]`, `txrisk retrain --if-drift`) retrains early when monitoring has found a drift action alert; `make retrain` runs it by hand. It runs in the compose `job` service as the owner, with `./models` mounted read-write.

- **Training set.** Transactions, SIM swaps and Fraud labels before the **cutoff**, the start of the simulated day the clock is in. Nothing after it is read, and Ground truth is never opened: the job takes no dataset path, only the database (ADR 0005, ADR 0007). A Fraud label counts from its discovery, and an unlabelled Transaction counts as legitimate only once it is older than the Label maturity window (ADR 0008). `txrisk seed` loads the labels discovered before day 90, so the first retrain has Fraud to learn from; later ones come from `fraud.discovered` events.
- **Splits slide with the cutoff** and keep the lengths in `config/evaluation.toml`: thresholds on the 30 days before the cutoff, calibration and the stacker on the 15 before those, models on everything earlier. At day 90 these are the first Champion's splits; at day 97 they are 0-52, 52-67 and 67-97. Features are rebuilt from Postgres by the same feature function, so a retrain at day 90 reproduces the offline Champion's version.
- **Challenger.** The bundle is saved in `models/<version>/`, logged to MLflow (alias `challenger`; `TXRISK_MLFLOW_URI`, `TXRISK_MLFLOW_ARTIFACTS`) and named in `challenger_model` in the same transaction that records the run in `retraining_runs`. A run holds a Postgres advisory lock, so the two jobs never train at once.
- **Early retraining.** A PSI at or above the action threshold (`psi` or `psi_alerted` in `monitoring_metrics`, the findings that fire the Alertmanager action alerts) on a monitored day since the last run's cutoff retrains, unless the last run was fewer than `drift.min_days_between_runs` days ago. The check reads the previous day's findings, so a drift is acted on within a simulated day of its alert.
- **Shadow scoring.** Workers and the API poll `challenger_model` every `TXRISK_CHALLENGER_REFRESH_SECONDS` (5) and load a new bundle from `TXRISK_MODELS_DIR` (default: the Champion bundle's folder), so a new Challenger scores within seconds without a restart. Every new Transaction is also scored with it from the same features: Risk score, band and components go to `challenger_scores`, apart from `risk_assessments`, never to an Alert, a Case or a response. A Challenger that raises, or is slower than the Champion's timeout, is recorded as an error row and cannot affect the Champion's assessment. `txrisk_challenger_scores_total{outcome}` counts them, and `challenger_scores` is kept 30 days.
- **Next.** A Challenger scores in shadow until an Admin promotes it: see below.

## Promotion, rollback and the model card

A Challenger model becomes the Champion model only when an **Admin** asks for it through the API and **both gates pass, checked by the server** at that moment (ADR 0012). Promotion is never automatic. The thresholds are in `config/promotion.toml`.

| Endpoint | Who | What |
|---|---|---|
| `GET /v1/models` | every role, or `models:read` | the Champion and Challenger, every retrained version, the latest Promotions and rollbacks |
| `GET /v1/models/{version}/card` | every role, or `models:read` | the model card, as data and as Markdown |
| `GET /v1/models/{version}/gates` | Admin | run both gates now, promoting nothing |
| `POST /v1/models/{version}/promote` | Admin | check both gates; if they pass, make it the Champion; if not, `409` with the report |
| `POST /v1/models/rollback` | Admin | put back the Champion the latest Promotion replaced, in one call |

- **Backtest gate.** The Champion and the Challenger both score the latest matured window (the 14 days ending one Label maturity window before the simulated time) from the model inputs the scoring path stored, against the Fraud labels known at that time (never Ground truth). The Challenger must match or beat the Champion on PR-AUC and on precision at the Alert budget, and no segment's false positive rate at High (region, KYC tier, Channel, Wallet age band) may rise by more than 20% relative. A window with fewer than `min_positives` known Fraud labels fails rather than passing on noise.
- **Shadow gate.** From `challenger_scores`: at least 7 simulated days between its first and latest score, its simulated Alert volume within 25% of the Alert budget, p95 latency within the sync API's 200 ms target, and zero scoring errors.
- **Promotion** moves the `champion_model` row and appends to `model_promotions` in the same transaction as its audit entry, with the gate results. Only the current Challenger can be promoted. Workers and the API poll the row (`TXRISK_CHALLENGER_REFRESH_SECONDS`, 5) and switch without a restart; `txrisk monitor` reads the Champion's baseline from it.
- **Rollback** is one step and has no gates. It is refused when the latest change was already a rollback, or there has been none. The rolled-back model stays the Challenger. Runbook: [docs/runbooks/model-rollback.md](docs/runbooks/model-rollback.md).
- **Audit.** `model.promote`, `model.rollback` and `model.gates` are in the audit log with who, the versions and the gate results; refused attempts (a denial, a failed gate) are recorded too. The application's database role can append to `model_promotions` and never change or delete it.
- **Model card.** Every bundle is saved with `model_card.json` and `model_card.md`: intended use, the data it was trained on (splits, Fraud labels known, Label maturity window, Alert budget), metrics by region, KYC tier, Channel and Wallet age band, and limitations. The segment metrics are against the labels known when the version goes live on the calibration days, which the stacker was fitted on, so they flatter the model; the backtest gate is the out-of-sample comparison. A bundle from before cards existed gets a card that says what was not recorded.

## Load tests

Locust scripts in [`loadtests/`](loadtests) exercise the sync API and the stream path, and `txrisk loadtest check` judges the results against the targets in [`config/loadtest.toml`](config/loadtest.toml): sync `/v1/score` at p95 < 200 ms at 200 requests/s on one API pod; the stream at 1,000 Transactions/s with 3 workers, lag back under 500 events within 60 s of a burst; and a Degraded mode run that keeps up at the same rate.

**Reproducing.** Bring up the stack on a machine with nothing else running (the 4 CPUs of a laptop are not enough to share), `make seed`, and train a bundle (`make train`, `TXRISK_MODEL_BUNDLE` in `.env`). Every run draws its Transactions from the dataset's last 20,000, under fresh IDs, so the Wallets are the seeded ones. Run each test once to warm the model, then for the record.

```bash
# Sync: a key that may score, with a rate limit above the offered rate, in a file git ignores.
mkdir -p reports/loadtest
docker compose exec -T api txrisk api-key --name loadtest --scope score:write --rate-limit 1000000 > reports/loadtest/api-key
make loadtest-sync                   # 200 requests/s for 2 minutes; fails any response that is Degraded
# Degraded mode: the same API with TXRISK_MODEL_BUNDLE unset (or pointing nowhere), then
LOADTEST_EXPECT_DEGRADED=1 make loadtest-sync RUN=sync-degraded   # fails any response that is not degraded
# Stream: three workers, then a producer at 1,500/s (a burst above the target); writes reports/loadtest/stream.json
docker compose up --detach --scale worker=3
make loadtest-stream LOADTEST_TIME=60s
make loadtest-check                  # PASS or FAIL for each target, exit 1 if any fails
```

A sync run is read at its steady state (`--reset-stats` drops the ramp-up). It passes only if the offered rate was reached, nothing failed, and the p95 held; a response whose `degraded` flag is not what the run expects counts as a failure, so a run against a healthy stack cannot quietly be scored by the rules alone. The stream run publishes at `LOADTEST_RATE` while a sampler reads the workers' committed offsets and the topic's end offsets each second; throughput is what the workers committed while publishing lasted, and recovery is the time after publishing stops until the lag first falls to the recovered level (waiting at most 3 minutes). Offsets are committed per batch of up to 500, so a short run reads low.

**Results.** Not yet at target. The only runs made so far were on a shared 4-CPU development machine whose load average was 8 to 9 throughout (other jobs were using the cores), against the API and workers as plain processes, not containers (the image build had no network), with Postgres, Redis and Redpanda in compose. They show where the time goes, not what a dedicated machine does, and none of the three targets was met:

| Run | Offered | Served | p95 | Verdict |
|---|---|---|---|---|
| Sync, Champion model, one API process | 200 req/s | 3.7 req/s | 17,000 ms | target missed |
| Sync, Degraded mode (no model), one API process | 200 req/s | 29.7 req/s, no failures, every response degraded | 2,200 ms | target missed |
| Stream, rules only, 3 workers | 1,515 Tx/s | about 29 Tx/s committed (peak lag 33,347) | | lag did not recover in 90 s; target missed |

Profiling the API under load found the time in two places: the model path (building the input frame, the Isolation Forest score and the SHAP contributions, a few hundred milliseconds of CPU for each Transaction) and the number of Postgres round trips each scoring makes one after another (parties, stored assessment, history, insert, simulation clock). Neither is a tuning knob: reaching the targets needs the scoring path made cheaper (a batch path for the workers, fewer round trips, cheaper explanations) and a dedicated machine, and the tables above should be replaced by a run on one when that is done.

## Kubernetes

The Helm chart in `deploy/helm/txrisk` deploys the whole stack to a local kind cluster, with probes and resource limits on every workload, an autoscaler for the scoring workers, and no credential in the chart, its values or the images: Secrets are created at deploy time from generated values.

```bash
make kind-up kind-deploy kind-smoke   # cluster, stack, then an end-to-end scoring smoke test
make kind-down
```

See [docs/runbooks/kubernetes.md](docs/runbooks/kubernetes.md).

## CI/CD

`.github/workflows/ci.yml` runs these jobs in parallel on every PR and push to main (uv's cache is on):

- `check`: ruff, `ruff format --check`, strict mypy and pytest with coverage. Coverage is gated at 80% on the **core logic only** (features, rules and scoring, the store's scoring and records, and the evaluation thresholds), as `[tool.coverage.report]` in `pyproject.toml` lists. Check it locally with `uv run pytest --cov`.
- `smoke`: the training smoke test, `generate -> features -> train -> evaluate` through the CLI on a tiny market (`uv run pytest -m smoke`).
- `audit`: `pip-audit` over the locked dependencies. It has no severity filter, so any known vulnerability fails the build.
- `trivy`: Trivy scans the three built images (the API's, the workbench's and the evaluation pages'), and the Dockerfiles, compose file and Helm chart, and fails on high and critical findings. Each image scan passes `--ignore-unfixed`: it skips vulnerabilities for which Debian has published no fixed version yet, because rebuilding cannot remove them (on 2026-10-28: 47 High findings in the Debian 13.7 base's util-linux, ncurses, systemd, acl and perl packages, all without a fix; the Python dependencies had none). The image builds run `apt-get upgrade`, so the gate fails as soon as a fix exists. The severity threshold is not lowered and there is no ignore file for the image scans or `pip-audit`; the one accepted configuration finding (`.trivyignore.yaml`, with its reason) is read-only root filesystems in the Helm chart.
- `contracts`: a breaking change to the OpenAPI spec (`oasdiff breaking`) or to any `schemas/*.json` event schema, compared with the base commit, fails the build. `contracts/openapi.json` is the committed snapshot of `/v1/openapi.json`; after an API change, regenerate it with `make openapi`. A test fails while the snapshot is stale.
- `compose`: the compose stack starts and reports ready; Prometheus scrapes the API and worker, Grafana loads its provisioned datasources and the Operations dashboard, and Jaeger receives the API's traces.
- `workbench`: in `workbench/`, `npm ci`, lint, strict typecheck, a check that the committed API types match `contracts/openapi.json`, the production build, Vitest and `npm audit --audit-level=high`; then the compose stack (API, Keycloak, Postgres, the workbench image) starts with throwaway settings and Playwright runs against it (Chromium is cached; the report is uploaded when it fails).
- `kubernetes`: lints and renders the Helm chart, builds the two images, deploys the whole stack to a kind cluster with Secrets generated for the run, scores a Transaction end to end, checks sign-in, the workers' consumer group, Prometheus, Grafana and the worker autoscaler (`deploy/kind/smoke.sh`), and deletes the cluster. See `docs/runbooks/kubernetes.md`.
- `restore`: a backup restores into a fresh database and matches its source.
- `publish`: on pushes to main, once the jobs above pass, pushes `ghcr.io/<owner>/<repo>:<commit sha>` using `GITHUB_TOKEN` (`packages: write`).

### Branch protection (repo owner)

Branch protection cannot be set from CI or by the agents that built it. In the repository, go to Settings > Branches > Add branch ruleset (or classic rule) for `main` and turn on:

- Require a pull request before merging.
- Require status checks to pass before merging, and require branches to be up to date. Add the checks `check`, `smoke`, `audit`, `trivy`, `contracts`, `compose`, `restore` and `workbench`.
- Block force pushes and deletion of `main`.

`publish` runs only on main, so it is not a required check.
