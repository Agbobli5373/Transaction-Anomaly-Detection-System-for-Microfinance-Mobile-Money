-- Scoped API keys for calling services. A key is `txrisk_<key_id>_<random part>`; only its SHA-256
-- digest is stored, so a database leak does not leak keys.

CREATE TABLE api_keys (
    -- The public part of the key, by which a presented key is looked up.
    key_id                text PRIMARY KEY,
    -- Which service holds it; a rotated key keeps its name.
    name                  text NOT NULL,
    -- SHA-256 of the whole key. The key itself is shown once, when issued, and never stored.
    key_digest            bytea NOT NULL CHECK (octet_length(key_digest) = 32),
    -- What it may do, e.g. {score:write, alerts:read}.
    scopes                text[] NOT NULL CHECK (cardinality(scopes) > 0),
    rate_limit_per_minute integer NOT NULL CHECK (rate_limit_per_minute > 0),
    created_at            timestamptz NOT NULL,
    -- The Admin who issued or rotated it, or the operator command that did.
    created_by            text NOT NULL,
    -- Null for a key that never expires. Rotation sets it on the old key, to the end of the overlap.
    expires_at            timestamptz,
    revoked_at            timestamptz,
    -- The key this one was rotated from.
    replaces              text REFERENCES api_keys
);

CREATE INDEX api_keys_by_name ON api_keys (name, created_at);
