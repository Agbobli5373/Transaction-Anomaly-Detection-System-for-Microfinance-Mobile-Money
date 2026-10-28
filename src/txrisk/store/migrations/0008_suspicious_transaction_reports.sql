-- Suspicious transaction reports: drafted from a Case closed as fraud, approved by a Supervisor,
-- and only then exportable. One per Case (a draft is rewritten in place until it is approved; an
-- approved report never changes). Times are event times (ADR 0005). The report holds only its
-- summary: the Transactions, Reasons and notes in the export file are read from the closed Case,
-- which can no longer change.

CREATE TABLE suspicious_transaction_reports (
    report_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id          bigint NOT NULL UNIQUE REFERENCES cases (case_id),
    status           text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
    summary          text NOT NULL,
    -- Who last wrote the summary, and when.
    drafted_at       timestamptz NOT NULL,
    drafted_by_id    text NOT NULL,
    drafted_by_name  text NOT NULL,
    -- Set exactly when the report is approved.
    approved_at      timestamptz,
    approved_by_id   text,
    approved_by_name text,
    CHECK ((status = 'approved') = (approved_at IS NOT NULL)),
    CHECK ((approved_at IS NULL) = (approved_by_id IS NULL))
);

-- An approved report is final: refuse any change to it, whoever makes it.
CREATE FUNCTION refuse_approved_report_change() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'approved' THEN
        RAISE EXCEPTION 'an approved Suspicious transaction report cannot be changed';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER approved_report_is_final
    BEFORE UPDATE ON suspicious_transaction_reports
    FOR EACH ROW EXECUTE FUNCTION refuse_approved_report_change();
