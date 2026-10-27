# Anomaly score (Isolation Forest)

An Isolation Forest trained on days 0–45 with no labels at all, judged on the same evaluation days (90–120) as the [rules-only baseline](rules-only.md). Its High band is set so that days 60–90 give the Alert budget of 100 a day. Here the Risk score is just the Anomaly score × 100. Combining it with the rules is #11.

Reproduce with:

```bash
make generate
uv run txrisk features --out data/features/features.parquet
uv run txrisk evaluate --scorer anomaly --features data/features/features.parquet
uv run txrisk evaluate --scorer lof --features data/features/features.parquet
```

Full numbers are in [anomaly/metrics.json](anomaly/metrics.json) and [lof/metrics.json](lof/metrics.json).

## Against the rules

| | Rules only | Anomaly score | LOF (comparison) |
|---|---|---|---|
| PR-AUC | **0.451** | 0.257 | 0.008 |
| ROC-AUC | 0.884 | **0.906** | 0.462 |
| High a day | 10.3 | 213 | 373 |
| Precision / recall at High | **0.89** / 0.28 | 0.08 / **0.54** | 0.01 / 0.09 |
| Precision at the Alert budget (top 100 a day) | **0.197** | 0.152 | 0.019 |
| Recall at High, against discovered labels | 0.43 | **0.61** | — |

![Precision-recall curve](anomaly/pr_curve.png) ![ROC curve](anomaly/roc_curve.png)

## By Fraud scenario (recall at High)

| Scenario | Rules only | Anomaly score |
|---|---|---|
| Account takeover drain | 0.62 | **0.69** |
| SIM-swap takeover | 0.32 | **0.66** |
| Social-engineering push | 0.10 | **0.74** |
| Agent structuring | 0.19 | **0.65** |
| Mule fan-in | **0.12** | 0.09 |
| Reversal scam | 0.00 | 0.00 |

## What this says

- **On its own, the Anomaly score ranks fraud worse than the rules** (PR-AUC 0.26 against 0.45), and it raises far more false alarms. Unusual is not the same as fraud.
- **It catches fraud the rules can't.** It finds 74% of Social-engineering pushes, against 10%, and 65% of Agent structuring, against 19%. It also holds up through the day-100 drift: SIM-swap takeover recall is 66%, where the rules fall to 32%. The two components find different fraud, which is the case for stacking them in #11.
- **It over-alerts, and Easter is the main reason.** The threshold was set to give 100 a day on days 60–90, but the evaluation days average 213. The extra is concentrated on days 92–96, which is 3–7 April, the Easter festive season the generator makes busier and spendier. Peak days reach 707 High Alerts. That's a Benign anomaly doing exactly what it was designed to do. Outside Easter it settles to about 85–220 a day, creeping up as the market grows. Drift monitoring (#20) exists to watch this kind of shift.
- **Mule fan-in and the Reversal scam stay invisible to it.** In both, each single Transaction looks ordinary: a victim pays a stranger a normal amount. Only the pattern across Wallets gives it away.
- **LOF is no better than chance here**, with ROC-AUC 0.46, even with every input rescaled first. Dropping the inputs that grow over time (Wallet age, history count) changed nothing: LOF stayed at chance and the Isolation Forest went to 185 High a day. LOF also has to compare every scored Transaction with its neighbours, so it was only ever an offline comparison. The Isolation Forest is the one that is served.
- **Segments:** the false positive rate at High is 0.7–2.4% across segments. It's highest for Wallets under 30 days old (2.4%). Being new is itself unusual, and no fraud in this window targets them. It's also higher in the App than on USSD (1.7% against 0.8%). The stacked score in #11 should be checked against these, not the raw Anomaly score.
