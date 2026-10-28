# Runbook: Degraded mode

Degraded mode means the Champion model bundle is missing, corrupt, failing, or slower than `TXRISK_SCORING_TIMEOUT_MS`, so the API and workers score from Rule hits alone (ADR 0009). Scoring continues. The assessments are marked `degraded: true` and `model_version: "rules-only"`, and their score and band are on the rules scale, not the model's. Critical rules still force High.

## Recognise it

- `GET /v1/ready` stays `200` and reports `degraded: true`, a `reason`, and `awaiting_rescore` (how many stored assessments wait for the model).
- Grafana Operations dashboard: the Degraded mode rate panel (`txrisk_scores_total{degraded="true"}`), plus the counters `txrisk_degraded_assessments_total{reason}` and `txrisk_model_load_failures_total`.
- Alert volume moves off the Alert budget, because rules-scale scores band differently. The Model health dashboard shows it.

```bash
curl -sS localhost:8000/v1/ready
```

## Find the cause

1. Read the `reason` in `/v1/ready`, then the API and worker logs (`docker compose logs api worker`; JSON lines, the model load failure is logged there).
2. Does `TXRISK_MODEL_BUNDLE` in `.env` point at a folder that exists inside the container (`/models/<version>`, mounted from `./models`)? `docker compose exec api ls /models`.
3. After a Promotion or rollback, does the Champion row name a version whose folder is not on the `models` volume? `GET /v1/models/active` (an access token, or a key with `models:read`) names it.
4. Timeouts: the first call after a load is slow. If the reason is a timeout under real load, the scoring path is the bottleneck (see the README's load-test results), not a broken bundle.

## Fix

- Put the bundle back, or correct `TXRISK_MODEL_BUNDLE`, then `docker compose up --detach api worker` to restart them. After a failure the model is skipped for `TXRISK_MODEL_RETRY_SECONDS` (30 by default) before it is tried again.
- If a bad Promotion caused it, follow [model-rollback.md](model-rollback.md).

## Recovery

Once the model is healthy, the API and workers re-score the degraded Transactions in the background and replace their stored assessments; the old ones stay in `risk_assessment_history`. An Alert already raised is kept and never duplicated. A Transaction that re-scores to High raises its Alert once.

```bash
docker compose run --rm job txrisk rescore --limit 5000   # on demand; exit code 3 means the model is still down
curl -sS localhost:8000/v1/ready                          # awaiting_rescore falls to 0
```

The scheduler also runs `txrisk rescore` daily (`[jobs.rescore-degraded]` in `config/scheduler.toml`). Done when `degraded` is false and `awaiting_rescore` is 0.

## What Degraded mode does not do

It does not stop Alerts: Critical rule hits and rules-scale High hits still raise them. An Analyst working a Case opened during Degraded mode should know its Risk score is on the rules scale until it is re-scored.
