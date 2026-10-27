"""Signing a demo user in from the command line, to call the API with curl.

The realm in `config/keycloak/txrisk-realm.json` has one demo user per role (`demo-analyst`,
`demo-supervisor`, `demo-admin`) and a `txrisk-demo-cli` client that allows Keycloak's direct
grant. Each demo user's sign-in value comes from `TXRISK_DEMO_SIGNIN_<ROLE>` in `.env`, which
Keycloak substitutes into the realm when it imports it; none is committed. People using the
workbench sign in through the browser instead.
"""

import json
import os
import urllib.parse
import urllib.request

DEMO_CLIENT = "txrisk-demo-cli"
# OAuth names the direct grant, and its form field, after the credential it carries.
_DIRECT_GRANT = "password"


def demo_signin_env(role: str) -> str:
    """The environment variable holding the demo user's sign-in value for `role`."""
    return f"TXRISK_DEMO_SIGNIN_{role.upper()}"


def demo_access(realm_url: str, role: str, *, sign_in: str | None = None) -> str:
    """An access token for the demo user of `role`, from the realm at `realm_url`.

    `sign_in` defaults to the value in the role's `TXRISK_DEMO_SIGNIN_<ROLE>` variable.
    """
    if sign_in is None:
        sign_in = os.environ[demo_signin_env(role)]
    form = {"grant_type": _DIRECT_GRANT, "client_id": DEMO_CLIENT, "username": f"demo-{role}"}
    form[_DIRECT_GRANT] = sign_in
    request = urllib.request.Request(
        realm_url.rstrip("/") + "/protocol/openid-connect/token",
        data=urllib.parse.urlencode(form).encode(),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return str(json.load(response)["access_token"])
