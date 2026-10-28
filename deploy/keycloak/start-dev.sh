#!/usr/bin/env bash
# Starts the development Keycloak, importing the txrisk realm, only once every demo user has a
# real sign-in value.
#
# The realm file names each demo user's credential as a ${TXRISK_DEMO_SIGNIN_<ROLE>} placeholder.
# Keycloak keeps a placeholder whose variable is unset as the literal text, which would give that
# user a publicly known credential, so this refuses to start instead. Development only: the demo
# users and the txrisk-demo-cli client do not belong in a production realm.
set -euo pipefail

minimum_length=12
problems=()
for role in ANALYST SUPERVISOR ADMIN; do
  name="TXRISK_DEMO_SIGNIN_${role}"
  value="${!name:-}"
  if [ -z "$value" ]; then
    problems+=("$name is not set")
  elif [ "${#value}" -lt "$minimum_length" ]; then
    problems+=("$name is shorter than $minimum_length characters")
  elif [[ "$value" == *'${'* ]]; then
    problems+=("$name holds a placeholder, not a value")
  fi
done

if [ "${#problems[@]}" -gt 0 ]; then
  for problem in "${problems[@]}"; do
    echo "refusing to start Keycloak: $problem (set it in keycloak.env; see keycloak.env.example)" >&2
  done
  exit 1
fi

exec /opt/keycloak/bin/kc.sh start-dev --import-realm "$@"
