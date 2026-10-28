-- Promotion, rollback and the Champion model pointer (#23). Nothing here holds Ground truth
-- (ADR 0007): the gates read only what the system itself stored.

-- Every Promotion and rollback, newest last, kept for ever: who changed the Champion model, to
-- what, from what, and the gate results the decision rested on. A rollback is restorable only
-- while it is the latest row's inverse, which is what makes it one step.
CREATE TABLE model_promotions (
    promotion_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action         text NOT NULL CHECK (action IN ('promote', 'rollback')),
    -- The Champion model's version before and after.
    from_version   text NOT NULL,
    to_version     text NOT NULL,
    -- The `champion_model` pointer before: null meant the bundle the processes are configured
    -- with, which is what a rollback to it puts back.
    from_pointer   text,
    -- Who did it (the audit log's actor id and name) and when, in real time.
    actor_id       text NOT NULL,
    actor_name     text NOT NULL,
    occurred_at    timestamptz NOT NULL,
    -- The backtest and shadow gate reports a Promotion passed; empty for a rollback.
    gates          jsonb NOT NULL DEFAULT '{}'
);

-- The Champion model: one row naming the bundle version every process scores with. Null means
-- the bundle each process is configured with (TXRISK_MODEL_BUNDLE), the first Champion; a
-- Promotion or rollback sets it, and processes poll it, so the change reaches them without a
-- restart.
CREATE TABLE champion_model (
    singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    bundle_version  text,
    promotion_id    bigint REFERENCES model_promotions
);

INSERT INTO champion_model (singleton) VALUES (true);
