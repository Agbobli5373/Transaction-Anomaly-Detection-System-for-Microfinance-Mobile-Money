import { createRemoteJWKSet, decodeJwt, jwtVerify } from "jose";

import type { Settings } from "./config";

/**
 * Keycloak sign-in: the OIDC authorization code flow with PKCE (S256), as a public client. The
 * browser is sent to `issuer` (the realm as it names it); the server exchanges and refreshes
 * through `internalIssuer` (the realm as the server reaches it).
 */

export type Credentials = {
  access: string;
  refresh: string;
  identity: string;
  /** Epoch milliseconds. */
  accessExpiresAt: number;
  refreshExpiresAt: number;
};

export const ROLES = ["analyst", "supervisor", "admin"] as const;
export type Role = (typeof ROLES)[number];

export type User = { sub: string; name: string; roles: Role[] };

function base64url(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

export function randomValue(bytes = 32): string {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

export async function challengeOf(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

export function redirectUri(s: Settings): string {
  return `${s.publicUrl}/auth/callback`;
}

export async function authorizationUrl(
  s: Settings,
  transaction: { state: string; nonce: string; verifier: string },
): Promise<string> {
  const url = new URL(`${s.issuer}/protocol/openid-connect/auth`);
  url.search = new URLSearchParams({
    client_id: s.clientId,
    response_type: "code",
    scope: "openid profile",
    redirect_uri: redirectUri(s),
    state: transaction.state,
    nonce: transaction.nonce,
    code_challenge: await challengeOf(transaction.verifier),
    code_challenge_method: "S256",
  }).toString();
  return url.toString();
}

export function endSessionUrl(s: Settings, identity: string | undefined, landing: string): string {
  const url = new URL(`${s.issuer}/protocol/openid-connect/logout`);
  const params = new URLSearchParams({
    client_id: s.clientId,
    post_logout_redirect_uri: `${s.publicUrl}${landing}`,
  });
  if (identity) {
    params.set("id_token_hint", identity);
  }
  url.search = params.toString();
  return url.toString();
}

type GrantResponse = {
  access_token?: unknown;
  refresh_token?: unknown;
  id_token?: unknown;
  expires_in?: unknown;
  refresh_expires_in?: unknown;
};

export type Fetch = typeof fetch;

// The form field that carries the refresh grant.
const REFRESH_FIELD = "refresh_token";

async function grant(
  s: Settings,
  form: Record<string, string>,
  fetchImpl: Fetch,
  now: number,
): Promise<Credentials> {
  const response = await fetchImpl(`${s.internalIssuer}/protocol/openid-connect/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: s.clientId, ...form }),
    cache: "no-store",
  });
  if (!response.ok) {
    // The body names the reason (an expired or revoked grant); it carries no credential, but is
    // still not logged: the status says enough.
    throw new Error(`the identity provider refused the grant (${response.status})`);
  }
  const body = (await response.json()) as GrantResponse;
  if (
    typeof body.access_token !== "string" ||
    typeof body.refresh_token !== "string" ||
    typeof body.id_token !== "string"
  ) {
    throw new Error("the identity provider's answer is missing a credential");
  }
  const seconds = (v: unknown, fallback: number) => (typeof v === "number" && v > 0 ? v : fallback);
  return {
    access: body.access_token,
    refresh: body.refresh_token,
    identity: body.id_token,
    accessExpiresAt: now + seconds(body.expires_in, 60) * 1000,
    refreshExpiresAt: now + seconds(body.refresh_expires_in, 1800) * 1000,
  };
}

export function exchangeCode(
  s: Settings,
  code: string,
  verifier: string,
  fetchImpl: Fetch = fetch,
  now: number = Date.now(),
): Promise<Credentials> {
  return grant(
    s,
    {
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(s),
      code_verifier: verifier,
    },
    fetchImpl,
    now,
  );
}

export function refreshCredentials(
  s: Settings,
  refresh: string,
  fetchImpl: Fetch = fetch,
  now: number = Date.now(),
): Promise<Credentials> {
  return grant(s, { grant_type: "refresh_token", [REFRESH_FIELD]: refresh }, fetchImpl, now);
}

/** The signed-in person: the identity JWT is verified (signature, issuer, audience, nonce); the
 * roles come from the access JWT's realm roles, which the API checks itself on every call. */
export async function userFrom(
  s: Settings,
  credentials: Credentials,
  nonce: string,
): Promise<User> {
  const keys = createRemoteJWKSet(new URL(`${s.internalIssuer}/protocol/openid-connect/certs`));
  const { payload } = await jwtVerify(credentials.identity, keys, {
    issuer: s.issuer,
    audience: s.clientId,
  });
  if (payload.nonce !== nonce) {
    throw new Error("the identity JWT's nonce does not match the sign-in");
  }
  if (typeof payload.sub !== "string") {
    throw new Error("the identity JWT has no subject");
  }
  const name =
    [payload.name, payload.preferred_username].find((v): v is string => typeof v === "string") ??
    "Signed-in user";
  return { sub: payload.sub, name, roles: rolesOf(credentials.access) };
}

export function rolesOf(access: string): Role[] {
  const claims = decodeJwt(access);
  const realm = claims.realm_access as { roles?: unknown } | undefined;
  const granted = Array.isArray(realm?.roles) ? realm.roles : [];
  return ROLES.filter((role) => granted.includes(role));
}
