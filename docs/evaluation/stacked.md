# Stacked Risk score

The model bundle (#11), judged on the same evaluation days (90–120 of the default dataset, seed 42) as the [rules-only baseline](rules-only.md), the [Anomaly score](anomaly.md) and the [supervised fraud probability](supervised.md). A logistic regression stacks the three components into one Risk score:

- **Rules:** the rules score, 0–1.
- **Anomaly:** the Anomaly score, 0–1.
- **Supervised:** the supervised model's raw probability, as log-odds.

The two models train on days 0–45. The stacker is fitted on days 45–60, with only the Fraud labels known on day 90 (ADR 0007, ADR 0008). The High band is set so that days 60–90 give the Alert budget of 100 a day, as for every other scorer, and Medium covers the next 3× that volume. Critical rule hits use up their share of the budget first, because they are High whatever their score.

Reproduce with:

```bash
make generate
uv run txrisk features --out data/features/features.parquet
uv run txrisk evaluate --scorer stacked --features data/features/features.parquet
# The same bundle, saved to models/<version>/ and recorded in MLflow (mlflow.db, mlruns/):
uv run txrisk train --features data/features/features.parquet
```

Full numbers are in [stacked/metrics.json](stacked/metrics.json).

## What the Risk score means

**The Risk score is the chance of discovered fraud × 100, not the chance of fraud.** The stacker learns from Fraud labels, and an undiscovered fraud counts as legitimate, so its probability inherits the same target as the supervised model's calibration. Here, only about two frauds in three are ever discovered. Correcting for that would need the discovery rate, which only Ground truth knows, and Ground truth may not reach training (ADR 0007). So no correction is applied.

At a fraud rate of about 0.2%, most scores are far below 1. High starts at a Risk score of **0.16**, and Medium at 0.094. The Risk score therefore keeps two decimals; rounded to whole numbers, nearly every alert would read 0. A Critical rule hit raises the score to at least the High threshold.

## The stacker's weights

| Component | Weight (log-odds per unit) |
|---|---|
| Rules score (0–1) | 7.37 |
| Anomaly score (0–1) | 1.29 |
| Supervised log-odds | 0.34 |
| Intercept | −6.28 |

- **The rules count for much more than their PR-AUC suggests.** One High-severity hit (a rules score of 0.5) adds 3.7 to the log-odds, and a Critical hit (0.9) adds 6.6. When a rule fires, it is right far more often than its coarse ranking shows.
- **The supervised model's log-odds are scaled down by about two thirds.** It is class-weighted, so its raw log-odds are overconfident. The stacker learns its own scaling for them, which is why it takes the raw probability rather than the calibrated one.
- **The Anomaly score adds a little.** Going from a typical Transaction (0.5) to the most unusual (1.0) adds about 0.65 to the log-odds.

## Against each component alone

| | Rules only | Anomaly score | Supervised only | Stacked |
|---|---|---|---|---|
| PR-AUC | 0.475 | 0.257 | 0.708 | **0.725** |
| ROC-AUC | 0.884 | 0.906 | 0.921 | **0.968** |
| High a day | 10.3 | 213 | 120 | 117 |
| Precision / recall at High | **0.89** / 0.28 | 0.08 / 0.54 | 0.22 / 0.82 | 0.23 / **0.83** |
| Precision at the Alert budget (top 100 a day) | 0.199 | 0.152 | 0.260 | **0.267** |
| Recall within the Alert budget (top 100 a day) | 0.61 | 0.47 | 0.80 | **0.82** |
| Recall at Medium or higher | 0.68 | 0.66 | **0.89** | 0.86 |
| Recall at High, against discovered labels | 0.43 | 0.61 | 0.88 | **0.90** |

![Precision-recall curve](stacked/pr_curve.png) ![ROC curve](stacked/roc_curve.png)

## By Fraud scenario

Recall at each scorer's own High band, and within the top 100 a day.

| Scenario | Fraud | Rules at High | Anomaly at High | Supervised at High | Stacked at High | Supervised, top 100 | Stacked, top 100 |
|---|---|---|---|---|---|---|---|
| SIM-swap takeover | 383 | 0.32 | 0.66 | **0.90** | 0.87 | **0.90** | 0.86 |
| Account takeover drain | 180 | 0.62 | 0.69 | 0.95 | **0.99** | 0.94 | **0.98** |
| Mule fan-in | 144 | 0.12 | 0.09 | 0.72 | **0.74** | 0.62 | **0.73** |
| Social-engineering push | 138 | 0.10 | 0.74 | 0.96 | **1.00** | 0.96 | **1.00** |
| Reversal scam | 81 | 0.00 | 0.00 | **0.41** | 0.38 | **0.40** | **0.40** |
| Agent structuring | 48 | 0.19 | **0.65** | 0.29 | 0.42 | 0.27 | **0.40** |

## Reliability: is the probability honest?

The stacker's own probability is the Risk score ÷ 100 before the Critical floor. Here it is checked just as the supervised probability was. Lower is better for both measures; ECE is the expected calibration error over ten equal-width bins.

| Against | Probability | Brier score | ECE |
|---|---|---|---|
| Eventual Fraud discovery (what both are fitted to) | supervised, calibrated | **0.000733** | **0.00048** |
| | stacked | 0.000832 | 0.00060 |
| Ground truth | supervised, calibrated | **0.001052** | **0.00105** |
| | stacked | 0.001190 | 0.00111 |

- **The stacked probability is a little less honest than the supervised one, though it ranks better.** It understates the mid-range. In its 0.1–0.2 bin, 45% of Transactions are eventually discovered as fraud, and 67% in its 0.2–0.3 bin. A logistic stacker can only scale and shift log-odds, while centred isotonic calibration can bend.
- **Against Ground truth it runs lower still.** That is the discovery gap from [What the Risk score means](#what-the-risk-score-means): the price of training only on what the operation would know (ADR 0007).
- If the Risk score is ever read as a probability rather than as a rank, recalibrating the stacked output on the calibration days with centred isotonic regression is the next step. Both judge scorers at the Alert budget, which depends only on rank.

## Reasons

Each Risk assessment gives up to three Reasons. Critical rule hits come first; after that comes whatever pushed the Risk score's log-odds up most:

- **A Rule hit** counts for its share of the rules' push, split by severity weight.
- **A model input** counts for its TreeSHAP contribution, weighted as the stacker weighs its model:
  - For the supervised model, this is LightGBM's own contribution to the raw log-odds.
  - For the Isolation Forest, SHAP explains the average path length. Unusualness is 2^(−path length / c), so each Transaction's rise in Anomaly score above a typical one is shared out in proportion to its path-length contributions.
- **Inputs that measure what a rule measures** name that rule. When the rule fired, the input's push joins the rule's Reason, in the rule's words, so one fact is never given twice; otherwise the input keeps its own code. For example, `receiver_fan_in_24h` joins `MULE_CASHOUT` when that rule fired, and reads as `RECEIVER_FAN_IN` on a Transfer the rule doesn't cover.
- **Messages state the value without judging it** (`WALLET_AGE`: "Wallet registered 400 days ago"), because the model can be pushed up by a long history as well as a short one. A count of zero is never a Reason.
- **Inputs that only say what kind of Transaction it is** (its type, channel and direction) give no Reason.

The riskiest evaluation Transactions, by scenario:

| Scenario | Risk score | Reasons |
|---|---|---|
| SIM-swap takeover | 99.34 | `SIM_SWAP_DRAIN` Moved 68% of the balance 12.2 hours after a SIM swap · `NEW_DEVICE_BURST` 3 Transfers to new Counterparties within an hour, from a device first used 57 minutes ago · `VELOCITY_24H` 1,580 GHS moved in the last 24 hours |
| Account takeover drain | 95.47 | `NEW_DEVICE_BURST` 3 Transfers to new Counterparties within an hour, from a device first used 15 minutes ago · `FIRST_TIME_LARGE_TRANSFER` Transfer of 6.1x the usual amount to a first-time recipient · `AMOUNT_VS_MEDIAN` Amount is 6.1x this Wallet's 30-day median |
| Mule fan-in | 91.63 | `MULE_CASHOUT` Cash-out after receiving from 17 different Wallets in 24 hours · `AMOUNT_VS_MEDIAN` Amount is 76.2x this Wallet's 30-day median · `SHARE_OF_BALANCE` Moves 100% of the balance |
| Social-engineering push | 84.37 | `FIRST_TIME_LARGE_TRANSFER` Transfer of 78.3x the usual amount to a first-time recipient · `AMOUNT` Amount of 5,000 GHS · `AMOUNT_VS_MEDIAN` Amount is 78.3x this Wallet's 30-day median |
| Agent structuring | 18.71 | `AGENT_STRUCTURING` 5 Cash-outs just under the limit at this Agent in 24 hours · `AMOUNT_VS_MEDIAN` Amount is 16.0x this Wallet's 30-day median · `AMOUNT` Amount of 978 GHS |
| Reversal scam | 15.47 | `FIRST_TIME_LARGE_TRANSFER` Transfer of 8.2x the usual amount to a first-time recipient · `AMOUNT_VS_MEDIAN` Amount is 8.2x this Wallet's 30-day median · `TIME_SINCE_LAST` 9 minutes since this Wallet's last Transaction |
| Legitimate | 68.59 | `SIM_SWAP_DRAIN` Moved 100% of the balance 0.1 hours after a SIM swap · `AMOUNT_VS_MEDIAN` Amount is 6.1x this Wallet's 30-day median · `DEVICE_AGE` From a device first used 100.3 days ago |

The legitimate one is a real owner who swapped their SIM and moved everything at once. It is exactly what `SIM_SWAP_DRAIN` exists to stop, and it is why that rule is Critical. A Critical rule hit is floored at the bundle's High threshold, not at the rules-only `critical_floor` of 90, because the stacked Risk score is on a different scale.

In a random 400 of the High alerts, every one had at least one Reason. The commonest first Reasons were:

- `AMOUNT_VS_MEDIAN` (201)
- `FIRST_TIME_LARGE_TRANSFER` (116)
- `DEVICE_AGE` (18)
- `RECEIVER_FAN_IN` (17)

## New Wallets

The first stacked bundle flagged legitimate Transactions from Wallets under 30 days old at High with a false positive rate of 1.11%, about 2.5× the other Wallet age bands (#45). No fraud in this window falls in that band. The cause was two rules, not the models or cold start.

**The evidence, from the first bundle on the evaluation days.** These are the 423 legitimate new-Wallet Transactions at High:

- **The rules pushed them over High.** 97% had a Rule hit: `FIRST_TIME_LARGE_TRANSFER` on 313 and `AMOUNT_VS_MEDIAN` on 298. One of the two was the first Reason on 399 of them (94%). Together the two give a rules score of 0.44, which adds 3.2 to the log-odds. That alone almost always clears High.
- **The models were not the cause.** On these Transactions the Anomaly score pushed less than on older Wallets' false positives (1.00 against 1.15 in log-odds). The supervised push was about the same (−2.51 against −2.65). Their SHAP pushes on `wallet_age_days` and `history_count` were −0.09 and +0.01, so the models, if anything, pulled new Wallets down.
- **Cold start was not the cause.** Only 1.2% of the 423 came from Wallets with fewer than 5 Transactions, where the features are empty. On the threshold days, cold-start Transactions were flagged at 0.08%.
- **Swapping one component at a time confirms it.** On the threshold days, each component was set to its typical value on older Wallets in turn. Of 380 new-Wallet false positives, 21 stayed High with the rules swapped, 219 with the supervised score swapped, and 330 with the Anomaly score swapped.

**Why those two rules mislead in a Wallet's first month.** Both compare an amount with the Wallet's own usual amount. A new Wallet has no settled usual amount yet, and nearly every Counterparty is new to it.

- On the threshold days, `FIRST_TIME_LARGE_TRANSFER` fired on 1.6% of new-Wallet Transactions, against 0.3% on older ones.
- Its hits are also far less often fraud. These are the training and calibration days, with the labels known on day 90:

| Rule | Hits on Wallets under 30 days | Fraud | Hits on older Wallets | Fraud |
|---|---|---|---|---|
| `FIRST_TIME_LARGE_TRANSFER` | 892 | 3 (0.3%) | 5,660 | 382 (6.7%) |
| `AMOUNT_VS_MEDIAN` | 1,824 | 3 (0.16%) | 21,222 | 426 (2.0%) |

- The base fraud rate is about 0.2%, so `AMOUNT_VS_MEDIAN` on a new Wallet is no evidence at all.
- Precision rises with Wallet age itself, not with the number of Transactions behind the median. On Wallets over 30 days old with only 5–10 prior Transactions, `AMOUNT_VS_MEDIAN` is right 1.4% of the time.
- The stacker can't learn this on its own. It sees one rules score, and its log-loss gains almost nothing from separating 0.3% from 0.05%. Given the rules score for new Wallets as a separate input, it weights that input at −0.07.

**The fix.** `FIRST_TIME_LARGE_TRANSFER` and `AMOUNT_VS_MEDIAN` now wait until the Wallet is 30 days old. This is `min_wallet_age_days` in `config/rules.toml`. The models, the stacker and the features are unchanged, and only what the system sees is used (ADR 0007). Every other rule still fires on new Wallets, including `SIM_SWAP_DRAIN` and `MULE_CASHOUT`.

**How it was chosen.** Each candidate was fitted on the calibration days and judged on the threshold days, against the fraud discovered by day 90. The evaluation days were scored once, at the end.

| Candidate (threshold days) | New-Wallet FPR ÷ other bands | 30–180d FPR | ≥180d FPR | PR-AUC |
|---|---|---|---|---|
| Before | 1.79 | 0.56% | 0.58% | 0.247 |
| Stacker also sees rules score × under 30 days | 1.72 | 0.56% | 0.58% | 0.247 |
| Stacker also sees rules score × log Wallet age | 1.15 | 0.38% | 0.69% | 0.261 |
| Only `FIRST_TIME_LARGE_TRANSFER` waits 30 days | 0.99 | 0.60% | 0.61% | 0.249 |
| **Both rules wait 30 days** | **0.14** | 0.65% | 0.66% | 0.249 |

The Wallet-age interaction flips the Anomaly score's weight negative, and it moves the rules' weight for every Wallet, not just new ones. Gating only `FIRST_TIME_LARGE_TRANSFER` would leave `AMOUNT_VS_MEDIAN` firing where it is no better than chance.

**The result on the evaluation days:**

| | Before | After |
|---|---|---|
| False positive rate at High, Wallets under 30 days | 1.11% (423 Alerts) | **0.13%** (49) |
| … ÷ the average of the other two bands | 2.45 | **0.24** |
| False positive rate at High, 30–180 days | 0.48% | 0.56% (+18%) |
| False positive rate at High, 180 days and over | 0.43% | 0.52% (**+22%**) |
| PR-AUC | 0.712 | 0.725 |
| Precision at the Alert budget | 0.265 | 0.267 |
| Recall at High | 0.82 | 0.83 |
| High a day | 113.7 | 116.6 |

**The trade-off.** The High threshold is set so the threshold days fill the Alert budget. Taking about 12 false Alerts a day away from new Wallets lowers the threshold, and those slots go to the next-riskiest Transactions, nearly all in older Wallets. So the false positive rate for Wallets 180 days and over rises by 22% relative, past the 20% limit set in #45. In absolute terms it ends at 0.52%, inside the 0.40–0.70% range of every other segment. That rise buys a little recall: 3 more frauds at High, all in older Wallets. Every other region, tier and channel moves by less than 14%.

The remaining 49 new-Wallet false positives have few Rule hits (12 of 49). They are led by `RECEIVER_FAN_IN` and `AMOUNT`, the same model pushes that put older Wallets' Transactions at High.

**One caveat about the synthetic market.** Fraud victims are drawn from Wallets that already exist, so no fraud here starts in a Wallet's first month. Real mule Wallets are often new. The rules that catch mules (`MULE_CASHOUT`, `NEW_DEVICE_BURST`) still fire on them, and so do both models. If new-Wallet fraud appears in Fraud labels, `min_wallet_age_days` should be revisited.

## Training and scoring time

Measured in one cloud container with nothing else heavy running:

| Step | Time |
|---|---|
| `txrisk train` with a saved feature table: both models, rule scores for 1.75M Transactions, stacker, thresholds, MLflow | 2 min |
| `txrisk evaluate --scorer stacked` with a saved feature table | 2 min |

Scoring one Transaction through `ScoringService.assess`, timed over 2,000 Transactions of a small market:

| | p50 | p95 |
|---|---|---|
| Rules only | 0.17 ms | 0.26 ms |
| Stacked, with Reasons | 50 ms | 67 ms |

Of the stacked time, about 15 ms goes on building the one-row model inputs, 14 ms on the components and the Risk score, and 16 ms on the SHAP contributions for the Reasons. This is well inside the 200 ms p95 target. The first assessment after loading a bundle also builds the Isolation Forest's SHAP explainer, which takes about 0.8 s once, so a server should warm it up before taking traffic.

## What this says

- **Stacking adds little to ranking but helps where the components disagree.** PR-AUC moves only from 0.708 to 0.725, but ROC-AUC rises from 0.921 to 0.969, and High holds as much fraud on fewer alerts (117 a day against 120). Most of the ranking already comes from the supervised model.
- **Agent structuring improves the most.** Recall at High rises from 0.29 to 0.42. The supervised model never sees a discovered structuring case. The rule and the Anomaly score both see this fraud, and the stacker learns to trust them.
- **SIM-swap takeover and the Reversal scam lose a little,** at 0.87 against 0.90 and 0.38 against 0.41. Both depend on the supervised model's generalisation:
  - The drifted SIM-swap pattern after day 100 waits too long for the SIM-swap rule to fire.
  - The Reversal scam never appears in training.

  The stacker scales the supervised model's log-odds down, so these lose some of their lead.
- **It still over-alerts at Easter.** High averages 117 a day, but it reaches 202–241 a day on days 92–95 (3–6 April), the busier, spendier festive season. Outside it, the daily count stays between 60 and 124.
- **Segments:**
  - The false positive rate at High is 0.40–0.70% across regions, tiers and channels.
  - For Wallets under 30 days old it is now 0.13%, down from 1.11%; see [New Wallets](#new-wallets).
  - Recall is lowest on the enhanced tier (0.61) and over USSD (0.79 against 0.86 on the App).
