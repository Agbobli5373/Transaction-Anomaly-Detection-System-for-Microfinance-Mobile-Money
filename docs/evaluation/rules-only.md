# Rules-only baseline

The eleven rules in `config/rules.toml`, on their own, judged on the evaluation split (days 90–120 of the default dataset, seed 42). This is the bar the models in #9–#11 have to beat.

Reproduce with:

```bash
make generate
make evaluate
```

Full numbers are in [rules-only/metrics.json](rules-only/metrics.json).

## Headline

| | |
|---|---|
| Transactions evaluated | 533,625 over 30 days |
| Fraud (Ground truth) | 974 Transactions (0.18%) |
| ROC-AUC / PR-AUC | 0.891 / 0.487 |
| **High band** | **403 Alerts, 13.4 a day: precision 0.83, recall 0.34**, false positive rate 0.013% |
| Medium or High | 17,174 (572 a day): precision 0.040, recall 0.70 |
| Precision at the Alert budget (top 100 a day) | 0.205 |
| Recall at High, against Ground truth | 0.34 |
| Recall at High, against discovered labels | 0.46 (of the 302 frauds the operation would have found by day 120) |

![Precision-recall curve](rules-only/pr_curve.png) ![ROC curve](rules-only/roc_curve.png)

## By Fraud scenario (recall at High)

| Scenario | Fraud Transactions | Recall |
|---|---|---|
| Account takeover drain | 180 | 0.62 |
| SIM-swap takeover | 383 | 0.32 |
| Agent structuring | 48 | 0.58 |
| Reversal scam | 81 | 0.48 |
| Mule fan-in | 144 | 0.12 |
| Social-engineering push | 138 | 0.10 |

## What this says

- **When the rules say High, they are nearly always right.** More than eight in ten High Alerts are fraud, but they find only about a third of it. The Alert budget allows 100 a day, and the rules fill only about 13 of those slots with confident Alerts.
- **Drift is already visible.** Over the whole 120 days the rules catch about 71% of SIM-swap takeovers at High. In this window it is 32%. From day 100, fraudsters wait 24–72 hours after the SIM swap and take 20–40% at a time, which slips under `SIM_SWAP_DRAIN` (within 24h, at least 50%). That is the drift monitoring and retraining in #20–#21 are meant to catch.
- **Rules can't see the victim's side.** For Mule fan-in, only the mule's Cash-outs fire; the victims' Transfers look ordinary on their own. For Agent structuring, `AGENT_STRUCTURING` now fires from the second near-limit Cash-out at an Agent, and `NEAR_LIMIT_CASH_IN` catches the loading Cash-ins that are far above anything the Wallet brought in lately (#46). The first structured Cash-out at each Agent, and Cash-ins of a size the Wallet already knows, still look ordinary.
- **Scams where the victim acts are hard for rules.** Social-engineering push happens from the victim's own phone and Channel, and only one medium rule (`FIRST_TIME_LARGE_TRANSFER`) touches it. The Reversal scam has a shape a rule can see: the victim sends money to a stranger who has just sent them some. `RETURN_TO_NEW_SENDER` (#46) catches nearly every one of those replies, and never fired before day 90. The bait Transfer itself, a small payment from an ordinary Wallet to a stranger, stays invisible, so about half of the scam's Transactions are out of any rule's reach. See [Reversal scam](stacked.md#reversal-scam).
- **The Medium band is too noisy to work.** It holds 17,125 Transactions with 3.9% precision. Thresholds derived from the Alert budget (#11) replace these fixed bands.
- **Segments:**
  - No segment stands out as unfairly flagged. The false positive rate at High is at most 0.018%, in the medium KYC tier.
  - No fraud in this window targets Wallets under 30 days old, because victims are drawn from Wallets that already existed.
  - `FIRST_TIME_LARGE_TRANSFER` and `AMOUNT_VS_MEDIAN` wait until a Wallet is 30 days old (#45). In its first month a Wallet has no settled usual amount, and nearly every Counterparty is new to it. Before this, the two rules put 1,664 Transactions from new Wallets in the Medium band, none of them fraud. See [New Wallets](stacked.md#new-wallets).
  - Recall is lower on USSD (0.30) than in the App (0.38).
