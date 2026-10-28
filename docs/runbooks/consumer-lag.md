# Runbook: consumer lag recovery

`txrisk worker` consumes `transaction.created` (and `sim_swap.recorded`, `fraud.discovered`) from Redpanda in the consumer group `txrisk-scoring` (`TXRISK_CONSUMER_GROUP`), scoring through the same idempotent store as the API. Lag is the end offset less the group's committed offset, per partition. It is exported as `txrisk_consumer_lag{topic,partition}` and shown as "Consumer lag" on the Operations dashboard.

Capacity is not proven. The load test did not reach its 1,000 Tx/s target: on a shared 4-CPU machine the workers committed about 29 Tx/s (README, Load tests). A producer faster than the workers builds lag that does not clear on its own, and more workers help only up to the partition count.

## Check

```bash
docker compose exec redpanda rpk group describe txrisk-scoring   # lag per partition, and which member owns it
docker compose ps worker                                         # are the workers up, or restarting?
docker compose logs --tail 100 worker                            # JSON lines; the reason a worker stopped
```

## Causes and fixes

| Symptom | Cause | Fix |
| --- | --- | --- |
| Lag grows on every partition, workers healthy | The offered rate is above what the workers score | Scale the workers: `docker compose up --detach --scale worker=3`. Three is the default partition count of `transaction.created` (`TXRISK_TOPIC_PARTITIONS`), so more would idle. On Kubernetes the worker autoscaler does this on CPU, one to three replicas |
| Lag on one partition, others fine | One Subject wallet's backlog, or a slow batch | Wait; offsets commit per batch of up to 500 |
| Workers restart in a loop and lag stays fixed | A failure that is not a bad event stops the worker without committing, and the batch is redelivered. Usual causes: Postgres or Redpanda unreachable, a migration pending | Read the worker log and fix the dependency (`docker compose ps postgres redpanda`, `GET /v1/ready`). The worker resumes from the committed offset |
| Rejected events in `txrisk_events_consumed_total` | Invalid events, or an unregistered party | They go to `<topic>.dlq` with the reason in an `error` header and do not block the partition. Inspect: `docker compose exec redpanda rpk topic consume transaction.created.dlq --num 5` |
| Lag together with degraded assessments | The model is failing or timing out | [degraded-mode.md](degraded-mode.md) |

## After recovery

Delivery is at least once. A redelivered event gets its stored assessment back and republishes `score.produced` unchanged: nothing is scored twice and no second Alert is raised. An event is committed only after its assessment is stored and its `score.produced` is confirmed, so lag never loses events. Check that lag has fallen to 0 and that Alert volume for the period is plausible against the Alert budget.

During a replay, a larger `SECONDS_PER_DAY` (`make replay SECONDS_PER_DAY=...`) slows the publishing if the workers cannot keep up.
