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

Each Transaction is scored with the clock at its own `occurred_at` (ADR 0005), against the history stored strictly before it, so the API gives exactly the offline scores. Set `TXRISK_MODEL_BUNDLE=/models/<version>` in `.env` to score with a bundle from `make train` (`./models` is mounted at `/models`); without one the rules alone score. Schema changes are plain SQL files in `src/txrisk/store/migrations/`, applied only by `txrisk migrate`, which compose runs as its own job before the API starts. Ground truth and Benign anomalies are never loaded into Postgres, and `seed` loads no Fraud discoveries: Fraud labels arrive only through `fraud.discovered` events (ADR 0007).

`make backup` takes a Postgres backup into the compose `backups` volume (a one-off job for a scheduler or host cron, pruned to the retention count in `config/backup.toml`); CI restores one into a fresh database and checks it against its source. Restore procedure: `docs/runbooks/restore.md`.

Outside Docker, `txrisk migrate`, `txrisk seed` and `txrisk serve` read the database from `TXRISK_DATABASE_URL`.

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
- **Retention** (`txrisk purge`, `config/retention.toml`) deletes rows older than each table's period, measured on the simulation clock, and the scheduler runs it daily. The audit log is configured to be kept longest (a validated rule) but does not exist yet (#15), so `purge` does not touch it.
