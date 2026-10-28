#!/usr/bin/env bash
# Deploys the stack to the current kubectl context (a kind cluster; see docs/runbooks/kubernetes.md).
#
#   deploy/kind/deploy.sh            # build nothing; install the chart with the images already loaded
#
# Environment: NAMESPACE (default txrisk), RELEASE (txrisk), IMAGE_TAG (dev, for both images), and
# EXTRA_HELM_ARGS (more `helm upgrade` arguments, e.g. `--set tracing.enabled=true`).
#
# The chart carries no credential. This script creates the Secrets it names, once, from freshly
# generated random values: a second run leaves them as they are, so the Postgres volume's password
# keeps matching. The values are written to a private temporary file, never to the terminal, a
# command line or the repository.
set -euo pipefail

namespace=${NAMESPACE:-txrisk}
release=${RELEASE:-txrisk}
tag=${IMAGE_TAG:-dev}
chart="$(cd "$(dirname "${BASH_SOURCE[0]}")/../helm/txrisk" && pwd)"

kubectl create namespace "$namespace" --dry-run=client --output yaml | kubectl apply -f -

# KEY=value for each key, all with the same value.
emit() {
  local value=$1 key
  shift
  for key in "$@"; do echo "$key=$value"; done
}

# create_secret NAME: the KEY=value lines on stdin become the Secret's entries.
create_secret() {
  local file
  file=$(mktemp)
  chmod 600 "$file"
  cat >"$file"
  kubectl --namespace "$namespace" create secret generic "$1" --from-env-file="$file" >/dev/null
  rm -f "$file"
}

if kubectl --namespace "$namespace" get secret txrisk-database >/dev/null 2>&1; then
  echo "keeping the Secrets already in namespace $namespace"
else
  owner=$(openssl rand -hex 16)
  app=$(openssl rand -hex 16)
  { emit "$owner" POSTGRES_PASSWORD PGPASSWORD; emit "$app" TXRISK_APP_DB_PASSWORD; } | create_secret txrisk-database
  emit "$app" TXRISK_APP_DB_PASSWORD | create_secret txrisk-app
  emit "$(openssl rand -hex 16)" GF_SECURITY_ADMIN_PASSWORD | create_secret txrisk-grafana
  emit "$(openssl rand -hex 32)" AUTH_SECRET | create_secret txrisk-workbench
  {
    for key in TXRISK_DEMO_SIGNIN_ANALYST TXRISK_DEMO_SIGNIN_SUPERVISOR TXRISK_DEMO_SIGNIN_ADMIN; do
      emit "$(openssl rand -hex 16)" "$key"
    done
  } | create_secret txrisk-keycloak
fi

# The autoscaler reads CPU from metrics-server, which a kind cluster does not ship. Its kubelets
# use self-signed certificates, hence --kubelet-insecure-tls (a local cluster only).
if ! kubectl --namespace kube-system get deployment metrics-server >/dev/null 2>&1; then
  kubectl apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml" >/dev/null
  kubectl --namespace kube-system patch deployment metrics-server --type=json \
    --patch '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
fi

# shellcheck disable=SC2086
helm upgrade --install "$release" "$chart" \
  --namespace "$namespace" \
  --set "image.txrisk.tag=$tag" \
  --set "image.workbench.tag=$tag" \
  --wait --wait-for-jobs --timeout 15m \
  ${EXTRA_HELM_ARGS:-}
