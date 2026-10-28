# Runbook: drift alerts

`txrisk monitor` runs once per simulated day (`make monitor` runs it by hand) and measures the Champion model against the **Training baseline** stored in its bundle. The Prometheus rules in `deploy/prometheus/rules/monitoring.yml` fire into Alertmanager (<http://localhost:9093>) and the alert sink. Thresholds are in `config/monitoring.toml`: a PSI of 0.1 warns and 0.25 needs action.

| Alert | Meaning |
| --- | --- |
| `InputDriftWarning`, `InputDriftAction` | A model input's PSI against the baseline (the `feature` label) |
| `RiskScoreDriftWarning`, `RiskScoreDriftAction` | The Risk score distribution moved |
| `AlertedInputDriftWarning`, `AlertedInputDriftAction` | The inputs of the last 7 days' High Transactions changed against the baseline's High Transactions. A change in how Fraud looks shows up here first |
| `AlertVolumeOutsideBudgetBand` | Daily Alerts outside 0.5x to 1.5x the Alert budget |
| `LabelPrecisionRegression`, `LabelRecallRegression` | Precision or recall at High fell for a segment, judged on matured Transactions and the Fraud labels known so far |
| `MonitoringHasNoBaseline` | The bundle predates baselines, so nothing is measured |

## Triage

1. Open Grafana, **Model health** (<http://localhost:3001>). Which input or segment, since which simulated day?
2. Look for an ordinary explanation. A festive day moves amounts and volume (the High threshold already rises with the market spend index, ADR 0011). A new region or Channel going live moves the mix. `history_count` and `device_age_seconds` are ignored by design.
3. Rule out a pipeline fault. A broken ledger feed (missing fields, a stuck producer, Degraded mode) looks like drift. Check `GET /v1/ready` and consumer lag first ([consumer-lag.md](consumer-lag.md), [degraded-mode.md](degraded-mode.md)).
4. Treat label-based regressions with care. Fraud is discovered late (ADR 0008), so recent days understate recall. The job judges only Transactions older than the Label maturity window, and a segment with fewer than `min_positives` known Fraud labels never alerts because its recall is noise.

## Act

- **Warning only:** note it and watch the next days.
- **Action alert:** the daily `[jobs.retrain-on-drift]` job (`txrisk retrain --if-drift`) retrains within a simulated day, unless the last run was fewer than `drift.min_days_between_runs` (3) days ago (`config/retraining.toml`). It registers a **Challenger model** that scores in shadow. `make retrain` runs it now.
- A Challenger never replaces the Champion by itself. An Admin promotes it only if both gates pass (backtest and shadow; `GET /v1/models/{version}/gates` shows them without promoting). See the README, Promotion, rollback and the model card.
- If the cause is a data fault, fix the feed and do not promote a model trained on the faulty days.
- If a model that was just promoted is the cause, see [model-rollback.md](model-rollback.md).

Alerts clear by themselves once a day's metrics are back under the thresholds. Silences are set in Alertmanager and are lost with its container.
