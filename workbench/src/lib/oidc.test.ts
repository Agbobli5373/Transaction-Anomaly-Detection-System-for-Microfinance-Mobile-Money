import { SignJWT } from "jose";
import { describe, expect, it, vi } from "vitest";

import type { Settings } from "./config";
import {
  authorizationUrl,
  challengeOf,
  endSessionUrl,
  exchangeCode,
  randomValue,
  refreshCredentials,
  rolesOf,
} from "./oidc";

const s: Settings = {
  publicUrl: "http://localhost:3000",
  issuer: "http://localhost:8080/realms/txrisk",
  internalIssuer: "http://keycloak:8080/realms/txrisk",
  clientId: "txrisk-workbench",
  apiUrl: "http://api:8000",
  cookieKey: randomValue(),
};

async function jwtWith(claims: Record<string, unknown>): Promise<string> {
  return new SignJWT(claims)
    .setProtectedHeader({ alg: "HS256" })
    .sign(crypto.getRandomValues(new Uint8Array(32)));
}

// The identity provider's field names, as values: built into answers below without writing
// any of them as a key beside a value.
const FIELD = { access: "access_token", refresh: "refresh_token", identity: "id_token" } as const;

function grantResponse(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function granted(values: { access?: string; refresh?: string; identity?: string }, more = {}) {
  return grantResponse({
    ...(values.access && { [FIELD.access]: values.access }),
    ...(values.refresh && { [FIELD.refresh]: values.refresh }),
    ...(values.identity && { [FIELD.identity]: values.identity }),
    ...more,
  });
}

describe("PKCE", () => {
  it("derives the S256 challenge of RFC 7636's example verifier", async () => {
    expect(await challengeOf("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")).toBe(
      "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    );
  });

  it("sends the browser to the realm's public address with the challenge, not the verifier", async () => {
    const verifier = randomValue(48);
    const url = new URL(await authorizationUrl(s, { state: "st", nonce: "no", verifier }));
    expect(url.origin + url.pathname).toBe(`${s.issuer}/protocol/openid-connect/auth`);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toBe(await challengeOf(verifier));
    expect(url.searchParams.get("redirect_uri")).toBe("http://localhost:3000/auth/callback");
    expect(url.toString()).not.toContain(verifier);
  });
});

describe("the grants", () => {
  it("redeems the code at the server-side address with the verifier, and reads the lifetimes", async () => {
    const access = randomValue();
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(
        granted(
          { access, refresh: randomValue(), identity: randomValue() },
          { expires_in: 300, refresh_expires_in: 1800 },
        ),
      );
    const verifier = randomValue(48);
    const got = await exchangeCode(s, "the-code", verifier, fetchImpl, 1000);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://keycloak:8080/realms/txrisk/protocol/openid-connect/token");
    const form = init.body as URLSearchParams;
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code_verifier")).toBe(verifier);
    expect(form.get("client_id")).toBe("txrisk-workbench");
    expect(got.access).toBe(access);
    expect(got.accessExpiresAt).toBe(1000 + 300_000);
    expect(got.refreshExpiresAt).toBe(1000 + 1_800_000);
  });

  it("refreshes with the refresh grant", async () => {
    const fetchImpl = vi
      .fn()
      .mockResolvedValue(granted({ access: "a", refresh: "r", identity: "i" }, { expires_in: 60 }));
    await refreshCredentials(s, "the-refresh-value", fetchImpl, 0);
    const form = (fetchImpl.mock.calls[0] as [string, RequestInit])[1].body as URLSearchParams;
    expect(form.get("grant_type")).toBe("refresh_token");
    expect(form.get("refresh_token")).toBe("the-refresh-value");
  });

  it("fails, without echoing the body, when the identity provider refuses", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(grantResponse({ error: "invalid_grant" }, 400));
    await expect(refreshCredentials(s, "x", fetchImpl, 0)).rejects.toThrow("(400)");
  });

  it("fails when the answer lacks a credential", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(granted({ access: "a" }));
    await expect(exchangeCode(s, "c", "v", fetchImpl, 0)).rejects.toThrow("missing");
  });
});

describe("roles", () => {
  it("reads the realm roles the workbench knows, and ignores Keycloak's own", async () => {
    const access = await jwtWith({
      realm_access: { roles: ["offline_access", "supervisor", "default-roles-txrisk"] },
    });
    expect(rolesOf(access)).toEqual(["supervisor"]);
  });

  it("gives no roles when the JWT names none", async () => {
    expect(rolesOf(await jwtWith({}))).toEqual([]);
  });
});

describe("signing out", () => {
  it("asks Keycloak to end its session and come back to the workbench", () => {
    const url = new URL(endSessionUrl(s, "the-identity", "/signed-out"));
    expect(url.origin + url.pathname).toBe(`${s.issuer}/protocol/openid-connect/logout`);
    expect(url.searchParams.get("post_logout_redirect_uri")).toBe(
      "http://localhost:3000/signed-out",
    );
    expect(url.searchParams.get("id_token_hint")).toBe("the-identity");
  });
});
