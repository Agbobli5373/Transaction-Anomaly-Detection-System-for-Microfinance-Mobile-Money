-- Cases: the unit of investigation. An Alert joins its Subject wallet's active Case (open or
-- investigating) or opens one; Analysts work the Case and close it as fraud or as legitimate.
-- Every time here is an event time (ADR 0005): a Case opens at its first Alert's Transaction and
-- moves on the simulation clock, never the wall clock.

CREATE TABLE cases (
    case_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_wallet_id text NOT NULL REFERENCES wallets (wallet_id),
    status            text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'investigating', 'closed_fraud', 'closed_legitimate')),
    opened_at         timestamptz NOT NULL,
    -- The Keycloak subject ID of the Analyst the Case is assigned to, and the Supervisor who did it.
    assignee_id       text,
    assignee_name     text,
    assigned_by       text,
    -- Set exactly when the Case is closed; a closed Case is never reopened. Together with the
    -- Alerts, notes and events, this is what a Suspicious transaction report is built from.
    closed_at         timestamptz,
    closed_by_id      text,
    closed_by_name    text,
    CHECK ((status IN ('closed_fraud', 'closed_legitimate')) = (closed_at IS NOT NULL))
);

-- At most one active Case per Subject wallet, whatever the concurrency: a second Alert that
-- finds none open races on this index and the loser joins the winner's Case.
CREATE UNIQUE INDEX cases_one_active_per_wallet
    ON cases (subject_wallet_id) WHERE status IN ('open', 'investigating');
CREATE INDEX cases_by_wallet ON cases (subject_wallet_id, case_id);
CREATE INDEX cases_by_assignee ON cases (assignee_id, case_id);
CREATE INDEX cases_by_status ON cases (status, case_id);
CREATE INDEX cases_by_time ON cases (opened_at);

ALTER TABLE alerts ADD COLUMN case_id bigint REFERENCES cases (case_id);

-- Alerts raised before Cases existed: one open Case per Subject wallet holding them all.
INSERT INTO cases (subject_wallet_id, opened_at)
SELECT subject_wallet_id, min(raised_at) FROM alerts GROUP BY subject_wallet_id;

UPDATE alerts SET case_id = cases.case_id
FROM cases WHERE cases.subject_wallet_id = alerts.subject_wallet_id;

ALTER TABLE alerts ALTER COLUMN case_id SET NOT NULL;
CREATE INDEX alerts_by_case ON alerts (case_id, alert_id);

-- What happened on a Case, in order: assignments, notes and transitions. Append-only in practice
-- (the application only inserts).
CREATE TABLE case_events (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id     bigint NOT NULL REFERENCES cases (case_id),
    kind        text NOT NULL CHECK (kind IN ('assigned', 'note', 'transition')),
    -- The simulation clock when it happened.
    occurred_at timestamptz NOT NULL,
    actor_id    text NOT NULL,
    actor_name  text NOT NULL,
    -- For a note, its text; for a transition, an optional comment.
    body        text,
    from_status text,
    to_status   text,
    -- For an assignment, the Analyst it assigned the Case to.
    assignee_id text
);

CREATE INDEX case_events_by_case ON case_events (case_id, event_id);

-- The transactional outbox: an event written in the same transaction as the change that caused
-- it, and published to Redpanda afterwards by `txrisk outbox`, so a crash between commit and
-- publish cannot lose it. Delivery is at least once; consumers are idempotent (a Fraud label is
-- stored once).
CREATE TABLE outbox (
    outbox_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The topic, without any deployment prefix.
    topic      text NOT NULL,
    key        text NOT NULL,
    -- The event exactly as it is published.
    payload    text NOT NULL,
    published  boolean NOT NULL DEFAULT false
);

CREATE INDEX outbox_unpublished ON outbox (outbox_id) WHERE NOT published;
