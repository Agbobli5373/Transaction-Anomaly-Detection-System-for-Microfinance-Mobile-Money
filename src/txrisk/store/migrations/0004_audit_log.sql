-- The audit log: who did what to which object, and how it ended. Append-only: the application's
-- database role (txrisk_app, see txrisk.store.roles) may only SELECT and INSERT, and the triggers
-- below refuse UPDATE, DELETE and TRUNCATE for everyone, the owner included (DELETE only through the
-- retention purge's controlled path), until the owner deliberately disables them. An entry never holds a secret: no token, API key or MSISDN.

CREATE TABLE audit_log (
    audit_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- When it happened. The entry's own event time when the action has one; for a request to the
    -- API, which has none, the operational wall clock at the request (ADR 0005 keeps scoring off
    -- the wall clock; an audit trail records real time).
    occurred_at timestamptz NOT NULL,
    -- 'person' signed in through Keycloak, 'service' calling with an API key, or 'operator' running
    -- a command with database access.
    actor_type  text NOT NULL CHECK (actor_type IN ('person', 'service', 'operator')),
    -- Keycloak's subject ID for a person, the API key ID for a service, the command for an operator.
    actor_id    text NOT NULL,
    -- The person's username or the API key's name: for reading, not for matching.
    actor_name  text NOT NULL,
    -- A person's roles when they acted; empty for a service or an operator.
    actor_roles text[] NOT NULL DEFAULT '{}',
    -- What was done, as a stable dotted code: transaction.view, api_key.issue, audit.read.
    action      text NOT NULL,
    -- What it was done to: its kind, and its ID where one thing was named (a listing has none).
    object_type text,
    object_id   text,
    -- 'success'; 'denied' (the caller lacked the permission, a 403); 'failed' (permitted, but it
    -- did not happen: no such object, or a conflict).
    outcome     text NOT NULL CHECK (outcome IN ('success', 'denied', 'failed')),
    -- Context that is safe to keep for good, such as an issued key's scopes and its ID. Never a secret.
    detail      jsonb NOT NULL DEFAULT '{}',
    CHECK (object_id IS NULL OR object_type IS NOT NULL)
);

CREATE INDEX audit_log_by_time ON audit_log (occurred_at, audit_id);
CREATE INDEX audit_log_by_actor ON audit_log (actor_id, audit_id);
CREATE INDEX audit_log_by_object ON audit_log (object_type, object_id, audit_id);
CREATE INDEX audit_log_by_action ON audit_log (action, audit_id);

-- Nobody but the owner has any right to it; the owner grants the application SELECT and INSERT.
REVOKE ALL ON audit_log FROM PUBLIC;

-- UPDATE and TRUNCATE are refused outright. DELETE is refused too, with one controlled path: the
-- retention purge (audit_log_purge, below), which only the owner may run. The trigger lets a DELETE
-- through only when the purge has flagged its own transaction AND the row is older than the floor
-- (a year), so even a forged flag cannot remove a recent entry, and no DELETE of any age is
-- possible without the flag. The application role has no DELETE privilege at all.
CREATE FUNCTION audit_log_refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
        AND current_setting('txrisk.audit_purge', true) = 'on'
        -- The floor is wall-clock time, like the entries' own timestamps: operational, not business
        -- logic (see tests/test_wall_clock.py).
        AND OLD.occurred_at < clock_timestamp() - interval '365 days' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_log is append-only: % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER audit_log_no_row_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_refuse_change();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_refuse_change();

-- The only way to delete audit entries: those older than `retention` (measured on the wall clock,
-- the clock the entries are stamped with), and never with a retention under a year. Returns how
-- many it deleted. Run by the owner role (the purge job); the application role cannot execute it.
CREATE FUNCTION audit_log_purge(retention interval) RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
    removed bigint;
BEGIN
    IF retention < interval '365 days' THEN
        RAISE EXCEPTION 'the audit log is kept at least 365 days, not %', retention
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    PERFORM set_config('txrisk.audit_purge', 'on', true);
    DELETE FROM audit_log WHERE occurred_at < clock_timestamp() - retention;
    GET DIAGNOSTICS removed = ROW_COUNT;
    PERFORM set_config('txrisk.audit_purge', 'off', true);
    RETURN removed;
END;
$$;

REVOKE ALL ON FUNCTION audit_log_purge(interval) FROM PUBLIC;
