#!/bin/sh
# Starts Grafana only once the admin password is set. Without one Grafana falls back to its
# publicly known default login, so refuse to start instead (set it in .env; see .env.example).
set -eu
if [ -z "${GF_SECURITY_ADMIN_PASSWORD:-}" ]; then
  echo "refusing to start Grafana: GF_SECURITY_ADMIN_PASSWORD is not set (see .env.example)" >&2
  exit 1
fi
exec /run.sh
