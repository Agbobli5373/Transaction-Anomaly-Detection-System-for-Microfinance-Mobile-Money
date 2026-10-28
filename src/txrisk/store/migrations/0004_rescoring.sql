-- Degraded mode: a Transaction scored with Rule hits alone (risk_assessments.degraded) waits to be
-- re-scored by the full model. The queue is that column itself; this index keeps finding the
-- waiting ones cheap however many assessments are stored.
CREATE INDEX risk_assessments_awaiting_rescore
    ON risk_assessments (assessed_at, transaction_id) WHERE degraded;

-- Re-scoring replaces the stored assessment; the one it replaced is kept here, so what a
-- Degraded-mode Transaction was first told stays on record. Not Ground truth: it is only ever an
-- earlier assessment of ours.
CREATE TABLE risk_assessment_history (
    history_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id   text NOT NULL REFERENCES risk_assessments,
    risk_score       double precision NOT NULL,
    risk_band        text NOT NULL,
    reasons          jsonb NOT NULL,
    component_scores jsonb NOT NULL,
    model_version    text NOT NULL,
    degraded         boolean NOT NULL,
    assessed_at      timestamptz NOT NULL,
    -- The model version whose assessment replaced this one.
    replaced_by      text NOT NULL
);

CREATE INDEX risk_assessment_history_by_transaction
    ON risk_assessment_history (transaction_id, history_id);
