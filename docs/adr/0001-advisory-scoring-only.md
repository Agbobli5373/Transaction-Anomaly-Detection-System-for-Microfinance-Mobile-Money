# Advisory scoring only

The system scores each Transaction synchronously and raises Alerts for people to review, but it never blocks, delays or reverses a Transaction and never declares one fraudulent. We rejected pre-authorization blocking because the system has no authority over real money movement and runs on synthetic data, so returning Allow/Block decisions would claim a guarantee we cannot back. A future contributor should not add a "block" outcome without revisiting this decision.
