# PaySim benchmark

An external check: the same scorers, judged with the same metrics, on [PaySim](https://www.kaggle.com/datasets/ealaxi/paysim1), a public synthetic mobile-money dataset we did not design (about 6.3M Transactions over 743 hourly steps, 0.13% fraud). Our own dataset comes from our own generator, so a scorer that does well on it may only have learned our generator. PaySim has different people, different fraud and a different author.

> **Results are pending.** The sandbox this was built in cannot reach Kaggle or any mirror of the dataset, and Kaggle needs credentials. The pipeline is built and tested end to end on a small fixture with PaySim's exact schema, but no PaySim numbers exist yet. The results sections below are left as placeholders; run the commands under [Reproduce](#reproduce) and fill them in from `reports/paysim/<scorer>/metrics.json`.

## Reproduce

```bash
# Needs Kaggle credentials (https://www.kaggle.com/settings -> API token).
uv run --with kaggle kaggle datasets download -d ealaxi/paysim1 -p data/paysim --unzip
# Builds the feature table once (a Python replay of 6.3M Transactions: expect an hour or more
# and several GB of memory), then judges every scorer and writes reports/paysim/<scorer>/.
uv run txrisk paysim --csv data/paysim/PS_20174392719_1491204439457_log.csv \
    --features data/features/paysim.parquet
# One scorer, reusing the saved feature table:
uv run txrisk paysim --csv data/paysim/PS_*_log.csv --features data/features/paysim.parquet --scorer stacked
```

`data/` and `reports/` are git-ignored; the dataset is never committed. Reports have the same files as `txrisk evaluate`'s, and `metrics.json` has the same keys plus a `benchmark` entry listing the mapping and everything left out.

## What was mapped

The pipeline is [`src/txrisk/benchmark/paysim.py`](../../src/txrisk/benchmark/paysim.py). It turns PaySim into the tables our generator writes, then runs the shared code: the one feature function, the rules, both models, the stacker, the Alert budget thresholds and the evaluation report. The only PaySim-specific code is the mapping and the list of what is left out.

### Transaction types

| PaySim | Our Transaction type | Direction | `nameDest` becomes |
|---|---|---|---|
| `CASH_IN` | Cash-in | in | Counterparty (`agent_id` column) |
| `CASH_OUT` | Cash-out | out | Counterparty (`agent_id` column) |
| `TRANSFER` | Transfer | out | Counterparty wallet |
| `PAYMENT` | Payment | out | Merchant |
| `DEBIT` | Cash-out | out | Counterparty (`agent_id` column) |

- **`DEBIT` is the one judgement call.** In PaySim it is money sent from the mobile-money account to a bank. Cash-out, value leaving the Wallet to the outside world, is the closest of our five types. It is not a Bill payment (our glossary keeps "Debit" away from that term). PaySim has no fraud among its `DEBIT` rows, so the choice cannot move a metric.
- **`nameDest` in `CASH_IN` and `CASH_OUT` is passed through as the Counterparty only.** PaySim's destinations there are not registered Agents with a known KYC tier, so every Agent-derived feature is left out (below).
- **`nameOrig` is the Subject wallet**, for every type including `CASH_IN`.

### Time

`step` is one hour, so step 1 is 00:00 of an arbitrary first day (1 January 2026; PaySim has no calendar). Hour of day and the gaps between Transactions mean something; the date does not. Transactions in the same step share a timestamp, and the features see only strictly earlier history, so **two Transactions in the same hour cannot see each other**. That is slightly more conservative than the hourly resolution needs, never less.

### Balances

- **`oldbalanceOrg` is used** as the Subject wallet's balance before the Transaction, for `share_of_balance_moved`. It is what the Wallet held at the time, so it is point-in-time.
- **`newbalanceOrig`, `oldbalanceDest` and `newbalanceDest` are never read, and neither is `isFlaggedFraud`.** The loader opens the CSV with only the seven columns it needs (`step`, `type`, `amount`, `nameOrig`, `oldbalanceOrg`, `nameDest`, `isFraud`), so they cannot reach a feature, a label or a score. A test fills them with values that give the labels away and checks that every table, feature and report is identical.
  - The balance-after fields describe the Wallet after the Transaction, so a model that reads them already knows what the Transaction did.
  - `oldbalanceDest` is suspect for a subtler reason: PaySim's destination balances are famously inconsistent with the amounts moved (zeroes, and balances that do not change when money arrives), and the pattern differs between fraud and legitimate rows. It looks like a property of the generator, not something an operation could know about a Counterparty.
  - `isFlaggedFraud` is the output of PaySim's own rule (large Transfers), so using it would be scoring with another scorer's answer.

## What was left out

PaySim has no SIM swaps, devices, regions, KYC tiers, Agents, channels or registration dates. Nothing is invented for them. The shared feature function still runs, because it needs placeholder values for its inputs, but each feature that depends on a placeholder is emptied afterwards, so no model or rule can use it.

| Left out | Why |
|---|---|
| Feature `seconds_since_sim_swap` | no SIM swaps |
| `device_age_seconds` | no devices |
| `new_region_30d` | no regions |
| `amount_to_kyc_limit` | no KYC tiers |
| `agent_cash_out_count_24h`, `agent_cash_out_amount_24h`, `agent_near_limit_cash_out_count_24h` | no Agents (the last also needs KYC tiers) |
| `wallet_age_days` | no registration dates |
| Model input `is_app` | no channel; always 0 |
| Rule `SIM_SWAP_DRAIN` (Critical) | no SIM swaps |
| Rule `NEW_DEVICE_BURST` | no devices |
| Rule `AGENT_STRUCTURING` | no Agents or KYC tiers |
| Rule `NEAR_KYC_LIMIT` | no KYC tiers |
| Rule `NEW_REGION` | no regions |
| Report segments region, KYC tier, channel, wallet age | none of them exist; the report is segmented by PaySim type instead |

**What remains:** the Subject wallet features (history count, velocity, amount against the Wallet's median, new Counterparty, unusual hour, night share), `receiver_fan_in_24h`, `share_of_balance_moved`, `new_counterparty_transfers_1h`, the Transaction's own amount, hour, type and direction, and four rules: `MULE_CASHOUT`, `FIRST_TIME_LARGE_TRANSFER`, `AMOUNT_VS_MEDIAN` and `ODD_HOUR`. Rule thresholds are exactly the ones in `config/rules.toml`; none was retuned for PaySim. Switching rules off is `Rules.disabled`, which is empty everywhere else.

## Splits, labels and the Alert budget

| | Ours | PaySim |
|---|---|---|
| Period | 120 days | 743 hourly steps, about 31 days |
| Train | days 0–45 (37.5%) | days 0–12 |
| Calibration (and the stacker) | 45–60 (12.5%) | 12–16 |
| Thresholds | 60–90 (25%) | 16–24 |
| Evaluation | 90–120 (25%) | 24–32 (about 7 days of data) |
| Alert budget | 100 a day, 0.56% of ~17,800 Transactions a day | 1,150 a day, the same share of ~205,000 a day |
| Label maturity window | 30 days | 0 |

The splits are ours cut in the same proportions on a 32-day axis, in `config/paysim.toml`. PaySim ends part-way through the 32nd day, so the evaluation split is a little short, and rates per day use the days it actually has. Everything is time-ordered, and the features are point-in-time as in the main evaluation: each row is computed from history strictly before it.

**PaySim's labels are complete; ours are delayed.** ADR 0007 keeps Ground truth away from the system: it learns only from Fraud discoveries, which arrive after a delay, and treats an unlabelled Transaction as legitimate only after the Label maturity window. PaySim gives `isFraud` for every row and says nothing about when anyone would have found out. So here:

- Every fraud counts as discovered the moment it happens, and the Label maturity window is 0. Training, calibration and the stacker therefore see every label before the evaluation split, which is a **more favourable setting than ours**, and the supervised model's results are not comparable with the main evaluation's for that reason.
- The two recalls our report shows, against Ground truth and against discovered labels, are identical here, and the reliability check against "eventual discovery" equals the one against Ground truth.
- Ground truth still never reaches a scorer: it lives in a separate table that only the report reads, as in the main evaluation.

## Results

Pending: no PaySim run has been made. Fill these in from `reports/paysim/<scorer>/metrics.json` once the dataset is available.

| | Rules only | Anomaly score | Supervised only | Stacked |
|---|---|---|---|---|
| Transactions evaluated | | | | |
| Fraud (Ground truth) | | | | |
| PR-AUC | | | | |
| ROC-AUC | | | | |
| High a day | | | | |
| Precision / recall at High | | | | |
| Precision at the Alert budget (top 1,150 a day) | | | | |
| Recall within the Alert budget | | | | |
| Recall at Medium or higher | | | | |

### By PaySim type

PaySim has no Fraud scenarios; its fraud is always a `TRANSFER` that drains an account, then a `CASH_OUT`. `recall_by_scenario` in `metrics.json` is therefore grouped by those two types.

| Type | Fraud | Rules at High | Anomaly at High | Supervised at High | Stacked at High |
|---|---|---|---|---|---|
| `TRANSFER` | | | | | |
| `CASH_OUT` | | | | | |

## What to look for when reading the results

These come from PaySim's published description, not from a run here, so check them against the numbers.

- **Most Wallets appear once.** PaySim's `nameOrig` is almost unique per row, so most Subject wallet features start cold (empty below 5 earlier Transactions) and the history-based rules rarely have anything to read. Expect `MULE_CASHOUT` and the median-based rules to fire rarely. The Counterparty side, where mules repeat, is where history exists.
- **Fraud lives in two types only.** `TRANSFER` and `CASH_OUT` carry all of it, so the Transaction type alone separates a lot. A high supervised score here is partly that.
- **A fraud Transfer empties the Wallet** (the amount equals `oldbalanceOrg`), which `share_of_balance_moved` sees. That is the same behaviour as our SIM-swap and takeover drains, so it is a fair feature, but it is also very regular in PaySim.
- **The Alert budget is a share of volume.** At 1,150 a day it is about 0.56% of Transactions, several times PaySim's 0.13% fraud rate, so precision at the budget is capped by how much fraud there is per day, not just by the scorer.
- **Do not read the supervised and stacked numbers as a like-for-like with the main evaluation.** Labels here are complete and instant; ours are not.

## Tests

`tests/test_paysim.py` covers, on a small fixture with PaySim's exact column names:

- the type mapping, Counterparty columns, directions, balance and step-to-time conversion, and that an unmapped type is refused;
- that the balance-after columns, `oldbalanceDest` and `isFlaggedFraud` are never read: poisoning them with label-revealing values, or deleting them, changes no table, feature, score or report;
- point-in-time features: truncating the data does not change earlier rows, and Transactions in the same hour cannot see each other;
- that unsupported features are empty and unsupported rules never fire, while the supported ones still do;
- `txrisk paysim` end to end: a report per scorer in the main evaluation's format, covering exactly the evaluation days, with the exclusions recorded.
