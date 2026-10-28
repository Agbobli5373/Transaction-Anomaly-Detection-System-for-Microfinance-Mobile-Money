-- The simulation clock, the scheduler's record of what it has fired, Fraud labels, and the indexes
-- the retention purge deletes by. Ground truth still never comes here (ADR 0007): a Fraud label
-- exists only because a Fraud discovery was published, and only from its event time.

-- Fraud labels. A row is the system's knowledge that a Transaction was fraudulent, dated by the
-- discovery's own event time. It is kept apart from `transactions` and has no foreign key to it:
-- a discovery may be consumed before its Transaction is stored, and the retention purge expires
-- the two on their own schedules. Readers must ask for the labels known as of a moment.
CREATE TABLE fraud_labels (
    transaction_id text PRIMARY KEY,
    discovered_at  timestamptz NOT NULL,
    source         text NOT NULL CHECK (source IN ('complaint', 'chargeback', 'case'))
);

CREATE INDEX fraud_labels_by_discovery ON fraud_labels (discovered_at);

-- The simulation clock (ADR 0005): the latest event time any worker or the API has seen. One row,
-- empty until the first event; it only ever moves forward.
CREATE TABLE simulation_clock (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    at        timestamptz
);

INSERT INTO simulation_clock (singleton) VALUES (true);

-- The last simulated period each scheduled job fired for, so a job fires once per period even
-- across restarts. `period` is the first day of the period in the scheduler's timezone.
CREATE TABLE scheduled_jobs (
    job_name   text PRIMARY KEY,
    period     date,
    -- The simulation clock when the job was launched.
    fired_at   timestamptz,
    -- What the launcher returned: a container name or a Kubernetes Job name.
    launch_ref text
);

-- Retention deletes each table by its event time.
CREATE INDEX transactions_by_time ON transactions (occurred_at);
CREATE INDEX sim_swaps_by_time ON sim_swaps (occurred_at);
CREATE INDEX risk_assessments_by_time ON risk_assessments (assessed_at);
CREATE INDEX alerts_by_time ON alerts (raised_at);
