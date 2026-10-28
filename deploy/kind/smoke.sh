#!/usr/bin/env bash
# Smoke test of the stack `deploy/kind/deploy.sh` installed: scores a Transaction end to end through
# the API, and checks the pieces around it (sign-in, the workers' consumer group, Prometheus,
# Grafana, the autoscaler). It reads every credential from the cluster's Secrets and prints none.
#
# Environment: NAMESPACE (default txrisk), IMAGE_TAG (dev).
set -euo pipefail

namespace=${NAMESPACE:-txrisk}
tag=${IMAGE_TAG:-dev}
k() { kubectl --namespace "$namespace" "$@"; }
secret_value() { k get secret "$1" --output "jsonpath={.data.$2}" | base64 --decode; }
retry() { # retry SECONDS COMMAND...
  local deadline=$((SECONDS + $1))
  shift
  until "$@"; do
    if [ "$SECONDS" -ge "$deadline" ]; then return 1; fi
    sleep 3
  done
}

forwards=()
cleanup() {
  for pid in "${forwards[@]}"; do kill "$pid" 2>/dev/null || true; done
  k delete job seed-smoke --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup EXIT

forward() { # forward SERVICE LOCAL_PORT REMOTE_PORT
  k port-forward "service/$1" "$2:$3" >/dev/null 2>&1 &
  forwards+=("$!")
  retry 30 curl --silent --output /dev/null "http://127.0.0.1:$2/"
}

echo "== every workload is ready"
for kind in deployment statefulset; do
  for workload in $(k get "$kind" --output name); do
    k rollout status "$workload" --timeout 300s
  done
done

echo "== seed the wallets a Transaction names"
k delete job seed-smoke --ignore-not-found >/dev/null
k apply -f - <<EOF >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: seed-smoke
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: seed
          image: txrisk:$tag
          imagePullPolicy: IfNotPresent
          command: [txrisk, seed-cases, --tag, smoke, --count, "1"]
          env:
            - name: TXRISK_DATABASE_URL
              value: postgresql://txrisk@postgres:5432/txrisk
          envFrom:
            - secretRef:
                name: txrisk-database
EOF
k wait --for=condition=complete job/seed-smoke --timeout 120s

echo "== score a Transaction through the API"
forward api 18000 8000
api=http://127.0.0.1:18000
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$api/v1/score" --request POST)" = 401
test "$(curl --silent --output /dev/null --write-out '%{http_code}' "$api/v1/alerts")" = 401
issued=$(k exec deployment/api -- txrisk api-key --name kind-smoke --scope score:write --scope transactions:read 2>/dev/null)
transaction='{
  "transaction_id": "T-smoke-0001",
  "occurred_at": "2030-06-02T10:00:00Z",
  "transaction_type": "transfer",
  "amount": "60.00",
  "subject_wallet_id": "w-smoke-000",
  "direction": "out",
  "subject_balance_before": "1000.00",
  "channel": "app",
  "device_id": "device-w-smoke-000",
  "region": "Greater Accra",
  "counterparty_wallet_id": "w-smoke-friend"
}'
score() {
  curl --fail --silent --show-error --dump-header /tmp/kind-smoke.headers \
    --header "X-API-Key: $issued" --header 'Content-Type: application/json' \
    --data "$transaction" "$api/v1/score"
}
assessment=$(score)
echo "$assessment" | python3 -c 'import json,sys; a = json.load(sys.stdin); assert a["transaction_id"] == "T-smoke-0001"; assert a["risk_band"] in ("Low", "Medium", "High"); print("scored:", a["risk_band"], a["risk_score"])'
# A repeat is the stored assessment, not a second scoring.
score >/dev/null
grep -qi '^idempotent-replayed: true' /tmp/kind-smoke.headers
rm -f /tmp/kind-smoke.headers
curl --fail --silent --header "X-API-Key: $issued" "$api/v1/transactions/T-smoke-0001" | grep -q '"T-smoke-0001"'

echo "== the API runs as the least-privilege role"
k exec deployment/api -- python -c '
import os
import sqlalchemy as sa
from txrisk.store import create_app_engine
with create_app_engine(os.environ["TXRISK_DATABASE_URL"]).connect() as c:
    assert c.execute(sa.text("select current_user")).scalar_one() == "txrisk_app"
'

echo "== sign-in: the Keycloak realm issues the demo Analyst a token the API accepts"
analyst=$(secret_value txrisk-keycloak TXRISK_DEMO_SIGNIN_ANALYST)
bearer=$(k run signin --rm --attach --quiet --restart=Never --image "txrisk:$tag" --image-pull-policy=IfNotPresent \
  --env "TXRISK_DEMO_SIGNIN_ANALYST=$analyst" -- txrisk demo-sign-in analyst --realm-url http://keycloak:8080/realms/txrisk)
curl --fail --silent --header "Authorization: Bearer $bearer" "$api/v1/alerts" >/dev/null

echo "== the scoring workers have joined their consumer group"
retry 120 bash -c "kubectl --namespace $namespace exec redpanda-0 -- rpk group describe txrisk-scoring 2>/dev/null | grep -q transaction.created"

echo "== Prometheus scrapes the API and the workers"
forward prometheus 19090 9090
targets_up() {
  local up
  up=$(curl --silent http://127.0.0.1:19090/api/v1/targets \
    | python3 -c 'import json,sys; t = json.load(sys.stdin)["data"]["activeTargets"]; print(*sorted(x["labels"]["job"] for x in t if x["health"] == "up"))')
  case " $up " in *" api "*" worker "*) return 0 ;; esac
  return 1
}
retry 120 targets_up
curl --fail --silent http://127.0.0.1:19090/api/v1/rules | grep -q AlertedInputDriftAction

echo "== Grafana has the provisioned dashboards"
forward grafana 13000 3000
admin=$(secret_value txrisk-grafana GF_SECURITY_ADMIN_PASSWORD)
retry 60 curl --fail --silent --user "admin:$admin" http://127.0.0.1:13000/api/dashboards/uid/txrisk-operations >/dev/null

echo "== the scoring workers have an autoscaler that reads their load"
cpu_known() { [ -n "$(k get hpa worker --output 'jsonpath={.status.currentMetrics[0].resource.current.averageUtilization}')" ]; }
retry 240 cpu_known

echo "kind smoke test passed"
