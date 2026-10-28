# Degraded mode scores on the rules scale and is re-scored in place

When the Champion model bundle is missing, corrupt, too slow or raising, the scoring service scores with Rule hits alone and marks the assessment `degraded` with `model_version = "rules-only"`. A degraded assessment uses the rules-only Risk score and the Risk bands from `config/rules.toml` (High at 50, a Critical rule at least 90), not the bundle's stacked scale (a probability x 100, High near 17), because the two are not comparable; the model version and the flag tell consumers which scale applies. Critical rules still force High.

The model is called through a circuit breaker with a timeout (`TXRISK_SCORING_TIMEOUT_MS`, default 5000, as the first call after a load is cold and takes seconds). A call that times out is abandoned on its pool thread, not killed, because Python cannot stop a thread; the pool is bounded, and after a failure the model is skipped for `TXRISK_MODEL_RETRY_SECONDS` (default 30) so a hung model does not add its timeout to every Transaction.

Degraded Transactions queue for re-scoring in Postgres itself: the queue is the stored assessments with `degraded = true` (a partial index keeps it cheap). `txrisk rescore` (for the scheduler), the API (a background task) and the worker (between batches) re-score them once the model has recovered. A re-scored assessment replaces the stored one; the replaced one is kept in `risk_assessment_history`.

Alerts follow the new band and never duplicate: a Transaction has at most one. A degraded High already has its Alert, so re-scoring adds none. A Low or Medium that re-scores to High raises exactly one. A High that re-scores lower keeps its Alert, because an Alert records that a Transaction was raised for review, which a better-informed score later does not undo; the Analyst sees the Alert with its now-lower assessment and closes it as any other. Alerts are never retracted automatically.

Readiness stays 200 while degraded, with `degraded: true` and the reason in the body: the service can still score, so a load balancer must keep sending traffic, while monitoring watches the flag and the `txrisk_degraded_assessments_total` counter.
