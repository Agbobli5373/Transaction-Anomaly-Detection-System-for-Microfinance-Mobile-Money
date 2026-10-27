# Ground truth never reaches the system

The generator knows every fraud perfectly, but that Ground truth is used only for final offline evaluation. The running system (API, Postgres, training, monitoring) sees only Fraud labels created by Fraud discoveries at their event time, and treats unlabelled Transactions as legitimate only after the 30-day Label maturity window. Even the first Champion model is trained on labels discovered by day 90 rather than on Ground truth. We accept a weaker first model because training on perfect labels would hide the label delay and selection bias every real fraud team faces, which would make both the models and the monitoring dishonest.

## Consequences

The evaluation report shows two recalls: against Ground truth (how much fraud really happened) and against Fraud labels (how much the operation found). The gap between them is a deliberate result, not a defect.
