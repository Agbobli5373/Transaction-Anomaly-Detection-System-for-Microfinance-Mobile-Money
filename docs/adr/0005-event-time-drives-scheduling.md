# Event time drives all windows and schedules

Every time-based behaviour (velocity windows, Fraud discovery, label maturity, retraining, drift reports, retention purges and the daily Alert budget) runs on event time, taken from Transactions' `occurred_at`, never on the wall clock. A simulation clock advances with the latest event time seen on the stream, and scheduled jobs fire when it crosses their schedule. We chose this because the live period is replayed in minutes: on wall-clock time, a 30-day replay would never trigger a weekly retrain. In a real deployment the simulation clock is simply the wall clock.

## Consequences

A "weekly" job may run every few minutes of wall-clock time during replay. That is intended, not a bug. No code may call `now()` for business logic; it must read the simulation clock.
