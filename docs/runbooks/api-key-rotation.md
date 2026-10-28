# Runbook: API key rotation and revocation

Services call `/v1` with `X-API-Key`. A key reads `txrisk_<key_id>_<random part>`, is shown once when issued, and only its SHA-256 digest is stored. Only an Admin (signed in through Keycloak) manages keys over the API; a key can never manage keys. Every issue, list, rotate and revoke is in the audit log.

## Routine rotation (no downtime)

```bash
# $BEARER is an Admin's access token; $API_URL is the API, e.g. http://localhost:8000
curl -sS "$API_URL/v1/api-keys" -H "Authorization: Bearer $BEARER"      # find the key_id; check "active"
curl -sS -X POST "$API_URL/v1/api-keys/$KEY_ID/rotate" \
  -H "Authorization: Bearer $BEARER" -H 'Content-Type: application/json' \
  -d '{"overlap_minutes": 1440}'                                        # the default overlap is a day
```

The response holds the new `key` (once) and its `api_key` record, with the same name, scopes, rate limit and expiry (rotation does not extend a key's life).

1. Store the new key where the service reads it (its secret store; never in git or a compose file).
2. Deploy the service with it within the overlap.
3. Confirm the old key is no longer used: `GET /v1/audit?actor=<old key_id>&since=<start of the overlap>` shows its last calls.
4. After the overlap the old key is refused.

A key is rotated once: a second attempt gets `409`, so rotate the successor next time. A revoked or expired key cannot be rotated (`409`); issue a new one.

## Leaked key

Revoke at once, then check what it did and issue a replacement:

```bash
curl -sS -X DELETE "$API_URL/v1/api-keys/$KEY_ID" -H "Authorization: Bearer $BEARER"
curl -sS "$API_URL/v1/audit?actor=$KEY_ID" -H "Authorization: Bearer $BEARER"
curl -sS -X POST "$API_URL/v1/api-keys" -H "Authorization: Bearer $BEARER" \
  -H 'Content-Type: application/json' -d '{"name": "ledger", "scopes": ["score:write"]}'
```

Revocation applies to the one key ID. If a rotation is inside its overlap and both keys may be exposed, revoke both IDs.

## No Admin available (the first key, or recovery)

The operator command writes straight into Postgres and is audited as an operator action:

```bash
docker compose exec -T api txrisk api-key --name ledger --scope score:write [--rate-limit N]
```

## Keys the stack itself holds

The evaluation pages hold a `models:read` key as `TXRISK_EVALUATION_API_KEY` in `.env`. After rotating it, update `.env` and run `docker compose up --detach evaluation-pages`. Load tests use a key written to `reports/loadtest/api-key` (git-ignored).

The audit log records who, what and the key's ID, never a key or a token.
