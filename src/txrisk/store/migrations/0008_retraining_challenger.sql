-- Retraining and Challenger shadow scoring (#22). Nothing here holds Ground truth (ADR 0007): a
-- retraining run reads only Transactions, SIM swaps and Fraud labels, as of its cutoff.

-- Every retraining run: what started it, the simulated time its training set was cut at, and the
-- bundle it produced. A drift-triggered run looks here for when the last one was.
CREATE TABLE retraining_runs (
    run_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    trigger           text NOT NULL CHECK (trigger IN ('weekly', 'drift')),
    -- Only Transactions before this time, and Fraud labels discovered before it, were used.
    cutoff            timestamptz NOT NULL,
    bundle_version    text NOT NULL,
    -- The version number MLflow registered the bundle as; null when MLflow was not used.
    registry_version  text,
    transactions      bigint NOT NULL,
    fraud_labels      bigint NOT NULL,
    -- Why it ran: e.g. the drift finding that triggered it.
    reason            text NOT NULL
);

-- The Challenger model: one row naming the bundle version that workers and the API score every
-- Transaction with in shadow. Retraining moves it; processes poll it, so a new Challenger is
-- picked up without a restart. Null until the first retraining run.
CREATE TABLE challenger_model (
    singleton       boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    bundle_version  text,
    run_id          bigint REFERENCES retraining_runs
);

INSERT INTO challenger_model (singleton) VALUES (true);

-- What a Challenger model scored each Transaction, kept apart from `risk_assessments` so a
-- Challenger score can never be read as, or become, the Champion's. It never raises an Alert.
-- A scoring that failed is recorded with its error and no score, for the shadow gate's error
-- count. Not keyed to `risk_assessments`: the retention purge deletes the two on their own terms.
CREATE TABLE challenger_scores (
    transaction_id   text NOT NULL,
    model_version    text NOT NULL,
    risk_score       double precision,
    risk_band        text,
    component_scores jsonb,
    error            text,
    -- How long the Challenger took to score, in milliseconds, for the shadow gate's latency check.
    latency_ms       double precision NOT NULL,
    assessed_at      timestamptz NOT NULL,
    PRIMARY KEY (transaction_id, model_version),
    CHECK ((error IS NULL) = (risk_score IS NOT NULL))
);

CREATE INDEX challenger_scores_by_version_time ON challenger_scores (model_version, assessed_at);
CREATE INDEX challenger_scores_by_time ON challenger_scores (assessed_at);
