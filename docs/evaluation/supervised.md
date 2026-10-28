# Supervised fraud probability

A class-weighted LightGBM model with isotonic calibration (#10), judged on its own on the same evaluation days (90–120 of the default dataset, seed 42) as the [rules-only baseline](rules-only.md) and the [Anomaly score](anomaly.md). It trains on days 0–45 and is calibrated on days 45–60, both with only the Fraud labels known on day 90 (ADR 0007, ADR 0008). As with the Anomaly score, its High band is set so that days 60–90 give the Alert budget of 100 a day, and Medium covers the next 3× that volume. The Risk score is the calibrated probability × 100. Combining it with the other components is #11.

Reproduce with:

```bash
make generate
uv run txrisk features --out data/features/features.parquet
uv run txrisk evaluate --scorer supervised --features data/features/features.parquet
```

Full numbers are in [supervised/metrics.json](supervised/metrics.json).

**Calibration is centred isotonic, not plain isotonic.** Plain isotonic regression is a step function. In the first run it gave only a few dozen distinct probabilities across half a million Transactions, which threw away the ranking inside each step. It also meant the Alert budget threshold could only take or leave a whole step, so High came out at about 2.5 times the budget. Replacing each step with its centre point and joining the centres with straight lines, starting from no fraud at a raw probability of 0, keeps the same fit to the labels and keeps the classifier's whole ranking.

## Against the other components

| | Rules only | Anomaly score | Supervised only |
|---|---|---|---|
| PR-AUC | 0.487 | 0.339 | **0.686** |
| ROC-AUC | 0.891 | **0.930** | 0.892 |
| High a day | 13.4 | 184 | 120 |
| Precision / recall at High | **0.83** / 0.34 | 0.10 / 0.58 | 0.21 / **0.78** |
| Precision at the Alert budget (top 100 a day) | 0.205 | 0.169 | **0.248** |
| Recall within the Alert budget (top 100 a day) | 0.63 | 0.52 | **0.77** |
| Recall at High, against discovered labels | 0.46 | 0.67 | **0.84** |

![Precision-recall curve](supervised/pr_curve.png) ![ROC curve](supervised/roc_curve.png)

**The supervised result moves with which frauds happen to be discovered.** Fixing the generator (Transactions no longer run past day 120, and each fraud episode now draws its own random report) left every evaluated Transaction the same, but it changed which earlier frauds were reported. PR-AUC went from 0.745 to 0.708. Expect this score to vary by a few hundredths between datasets for that reason alone.

**The inputs added in #46 move it again.** `amount_to_largest_cash_in_30d` and `received_from_counterparty_24h` joined the shared feature function for two new rules, and every model sees every feature. Before them, PR-AUC was 0.708 and recall at High 0.82. On the threshold days, against the fraud discovered by day 90, the new inputs raise PR-AUC from 0.246 to 0.283 but lower ROC-AUC from 0.950 to 0.929, and precision at the Alert budget is unchanged. All of that comes from `amount_to_largest_cash_in_30d`: `received_from_counterparty_24h` is almost always 0 before day 100 and changes nothing. On the evaluation days the model alone is a little worse (PR-AUC 0.686), while the stacked Risk score is better (0.753 against 0.725). See [stacked.md](stacked.md#agent-structuring-and-the-reversal-scam).

## By Fraud scenario

Recall at each scorer's own High band, and within the top 100 a day. The second is the fairer comparison, because the rules' High band holds only about 13 a day. The rules' scores are coarse, so which of their tied Transactions fall inside the top 100 depends on Transaction order.

| Scenario | Fraud | Rules at High | Anomaly at High | Supervised at High | Rules, top 100 | Anomaly, top 100 | Supervised, top 100 |
|---|---|---|---|---|---|---|---|
| SIM-swap takeover | 383 | 0.32 | 0.69 | **0.83** | 0.52 | 0.60 | **0.81** |
| Account takeover drain | 180 | 0.62 | 0.74 | **0.95** | 0.83 | 0.68 | **0.94** |
| Mule fan-in | 144 | 0.12 | 0.11 | **0.84** | 0.40 | 0.09 | **0.80** |
| Social-engineering push | 138 | 0.10 | 0.85 | **0.94** | **0.99** | 0.80 | 0.94 |
| Reversal scam | 81 | **0.48** | 0.02 | 0.06 | **0.51** | 0.01 | 0.09 |
| Agent structuring | 48 | 0.58 | **0.67** | 0.38 | **0.60** | **0.60** | 0.25 |

## Reliability: is the probability honest?

Calibration is fitted to Fraud labels, and an undiscovered fraud counts as legitimate. So the calibrated probability estimates **the chance of fraud that gets discovered**, not the chance of fraud. In this window, 634 of the 974 frauds are ever discovered (65%). The report checks reliability against both targets. Lower is better for both measures; ECE is the expected calibration error over ten equal-width bins.

| Against | Probability | Brier score | ECE |
|---|---|---|---|
| Eventual Fraud discovery (what calibration targets) | raw | 0.000776 | 0.00066 |
| | **calibrated** | **0.000738** | **0.00056** |
| Ground truth | **raw** | **0.000837** | **0.00072** |
| | calibrated | 0.001051 | 0.00113 |

- **Against what it was fitted to, calibration helps a little.** Brier falls 5% and ECE by 14%. That is less than in the first run, where the raw probability overstated the mid-range more.
- **Against Ground truth, calibration makes it worse.** Fraud is found only about two times in three, so the calibrated probability runs low. In its 0.4–0.5 bin, 96% of Transactions are really fraud. That is the label-delay gap ADR 0007 accepts on purpose, showing up in the probability itself.
- **The raw probability is close to Ground truth by luck, not design.** Most fraud here is cleanly separable, so the trees put it near 1 and almost everything else near 0. Of the 460 Transactions above 0.9 raw, 97% are fraud.
- **What to do about it is a decision for #11.** The stacker is refitted on days 45–60 with the same labels, so it inherits the same target. If the Risk score should mean "chance of fraud" rather than "chance of discovered fraud", it needs a discovery-rate correction. Nothing else can supply that, because Ground truth may not reach training.

## Training and scoring time

Measured on one laptop with nothing else heavy running:

| Step | Time |
|---|---|
| Feature table: all 1.75M Transactions replayed in time order (`txrisk features`) | 11–13 min |
| Training: labels, model inputs, LightGBM (100 trees) and calibration | 72–87 s |
| Batch scoring of the 533,625 evaluation Transactions | 5.4 s (about 10 µs each) |
| `txrisk evaluate --scorer supervised` with a saved feature table | under 2 min |

Scoring one Transaction through `ScoringService.assess`, timed over 2,000 Transactions of a small market:

| | p50 | p95 |
|---|---|---|
| Rules only | 0.27 ms | 0.63 ms |
| With the supervised component | 68 ms | 160 ms |

Nearly all of that difference is building the one-row model inputs (`inputs_for`, about 60 ms), not the model: the trees and calibration together take about 0.5 ms. `assess` builds them once when it has both the anomaly and supervised models. Serving latency work belongs in the shared model inputs.

## What this says

- **The model ranks fraud far better than either other component.** PR-AUC rises to 0.686, against 0.487 for the rules and 0.339 for the Anomaly score. Filling the same 100 Alerts a day, it finds 77% of fraud, against 63% for the rules.
- **It learns what the rules see from only one side.** Mule fan-in recall rises from 0.12 to 0.84, because the model also learns the victims' Transfers that look ordinary to any single rule.
- **It holds up through the day-100 drift.** SIM-swap takeover recall is 0.83, against the rules' 0.32, even though the drifted pattern (waiting 24–72 hours, taking 20–40%) never appears in its training days.
- **It barely catches a scam it has never seen.** The Reversal scam starts on day 100, after every training day. Before #46, 41% of it was High, most likely because a large Transfer to a stranger looked like the Social-engineering pushes the model did learn. With the new inputs it is 6%. Which resemblance a model happens to find for a scenario it never saw is fragile, and that is why `RETURN_TO_NEW_SENDER` carries this scam in the stacked Risk score instead.
- **It can't learn what is never discovered.** No victim complains about Agent structuring, so its discovery chance is 5%, and in this dataset none of its 484 Transactions is ever reported. The model has no structuring examples to learn from, and its 0.38 recall comes only from resemblance to other fraud. That is below the Anomaly score's 0.67. The components find different fraud, which is the case for stacking them in #11.
- **It over-alerts at Easter, like the Anomaly score.** High averages 120 a day against the budget of 100 set on days 60–90. The peak is days 92–95 (3–6 April), at 153–211 a day: the busier and spendier festive season. Outside it, the daily count stays at about 78–166.
- **Segments:**
  - The false positive rate at High is 0.36–1.13% across segments. It's highest for Wallets under 30 days old, where no fraud falls in this window, and on the minimum KYC tier (0.68%).
  - Recall is lowest on the enhanced tier (0.62).
  - USSD and App are close (0.75 and 0.81).
