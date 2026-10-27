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
| `fraud.discovered` | A Fraud discovery (complaint, chargeback or Case); no producer yet | transaction ID |

The versioned JSON Schemas in [`schemas/`](schemas) are the documented ledger contract. They are written from the Pydantic models that validate every event at both ends (`make schemas`), and a test fails if the two disagree.

- **Exactly once by transaction ID.** Workers score through the same idempotent store as `POST /v1/score`, and commit a batch's offsets only after its assessments are stored and its `score.produced` events are confirmed. A redelivered or replayed event gets the stored assessment back and republishes it unchanged, so `score.produced` may repeat a result, never change it. Only the High Risk band raises an Alert, once.
- **The same scores as the API.** Both paths score each Transaction with the clock at its `occurred_at` against the history stored before it, so they give identical assessments.
- **SIM swaps** after the seeded history arrive on `sim_swap.recorded`. Before scoring a batch, a worker stores every SIM swap on that topic, and the producer has each SIM swap confirmed before it publishes any later Transaction, so a Transaction's features always see the SIM swaps before it.
- **Ordering.** Transactions are keyed by Subject wallet, so each Subject wallet's own history is scored in order. Histories across Subject wallets (Transfers into a Wallet, an Agent's Cash-outs) span partitions, so with several workers they are as complete as the other partitions have got.
- **Rejected events** (invalid against the contract, or naming an unregistered party) go to `<topic>.dlq` with the reason in an `error` header. Any other failure stops the worker without committing, and its batch is redelivered.

Outside Docker, `txrisk worker` reads `TXRISK_DATABASE_URL` and `TXRISK_KAFKA_BOOTSTRAP_SERVERS` (and optionally `TXRISK_MODEL_BUNDLE`, `TXRISK_TOPIC_PARTITIONS`, `TXRISK_CONSUMER_GROUP`), and `txrisk produce` takes `--bootstrap-servers`.
