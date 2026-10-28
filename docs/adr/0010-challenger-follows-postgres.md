# Retraining names the Challenger model in Postgres, and splits slide with the cutoff

Retraining saves each bundle under the models folder and then names its version in a one-row Postgres table (`challenger_model`), in the same transaction that records the run. Workers and the API poll that row and load the bundle the first time they see a new version, so a Challenger registered while they run scores in shadow within seconds, without a restart. We chose Postgres over polling the MLflow registry or a watched file because every process already depends on it, the pointer and the run record change atomically, and a bundle that is named is always already saved. MLflow still records every run and gives the new version a `challenger` alias, as the record of how it was made; it is not on the scoring path.

A Challenger's scores go to their own table and never into `risk_assessments`, an Alert or a response, so a model nobody has promoted cannot be mistaken for the Champion or raise work for Analysts. Its failures are stored as rows with an error and no score, which the shadow gate will count, and cannot change the Champion's assessment.

Retraining keeps the lengths of the splits in `config/evaluation.toml` (ADR 0008) and slides them so the live period starts at the cutoff: thresholds on the last days before it, calibration and the stacker on the days before those, the models on everything earlier. Only the days the Label maturity window has matured carry labels, so the labelled windows end 30 days before the cutoff and the models see more history each week.

## Consequences

Retraining recomputes every feature from Postgres with the same feature function, which takes minutes at the full dataset's size; it is a scheduled job, not a request. A drift-triggered run acts on the previous simulated day's findings, because the scheduler fires the check as the clock enters a day.
