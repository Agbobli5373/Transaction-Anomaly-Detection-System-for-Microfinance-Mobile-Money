# Runbook: roll back a Promotion

Use this when a model promoted to Champion is scoring badly: Alert volume far outside the Alert budget, a segment flooded with Alerts, a wave of scoring errors or Degraded mode, or a drift alert on a model that was only just promoted. A rollback needs an Admin signed in (an API key cannot do it) and takes one call.

## Decide

1. Open the Model health dashboard (Grafana). Does the trouble start when the Promotion took effect? `GET /v1/models` lists the latest Promotions and rollbacks with who made them and when, newest first.
2. If it does, roll back. Do not wait for the gates: a rollback has none, so that it is quick.

## Roll back

```bash
# $BEARER is an Admin's access token (sign in through the workbench's Keycloak client)
curl -sS -X POST "$API_URL/v1/models/rollback" -H "Authorization: Bearer $BEARER"
```

The response names the version put back (`to_version`) and the one taken out (`from_version`). The workers and the API follow the change within `TXRISK_CHALLENGER_REFRESH_SECONDS` (5 by default), without a restart. Check:

```bash
curl -sS "$API_URL/v1/models/active" -H "Authorization: Bearer $BEARER"   # model_version is the one put back
```

## What it does and does not do

- It restores the Champion model the **latest** Promotion replaced. It is one step: a second call is refused (`409`) until there is another Promotion. To go back further, promote the older model again, which needs it to be the Challenger and to pass both gates.
- The rolled-back model stays the Challenger model and keeps scoring in shadow, so it can be examined, and promoted again if it passes the gates later.
- Alerts raised while it was Champion stay raised, with the Risk scores it gave. Re-scoring them with the restored model is not automatic.
- It is audited as `model.rollback` with who and the two versions. `model_promotions` keeps the whole history, and the application's database role cannot change it.
