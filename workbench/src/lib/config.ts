/** The workbench's settings, read from the environment when first needed (never at build time). */

export type Settings = {
  /** Where browsers reach the workbench: the OIDC redirect and the cookies' scope follow it. */
  publicUrl: string;
  /** The realm as browsers (and the tokens' `iss`) name it. */
  issuer: string;
  /** The same realm as the Next.js server reaches it; differs from `issuer` inside compose. */
  internalIssuer: string;
  clientId: string;
  /** The txrisk API, as the Next.js server reaches it. */
  apiUrl: string;
  /** The key material the browser's session cookie is encrypted with. */
  cookieKey: string;
};

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set`);
  }
  return value;
}

function trimmed(url: string): string {
  return url.replace(/\/+$/, "");
}

let cached: Settings | undefined;

export function settings(): Settings {
  if (cached) {
    return cached;
  }
  const issuer = trimmed(required("OIDC_ISSUER"));
  const material = required("AUTH_SECRET");
  if (material.length < 32) {
    throw new Error("AUTH_SECRET must be at least 32 characters (e.g. `openssl rand -hex 32`)");
  }
  cached = {
    publicUrl: trimmed(process.env.WORKBENCH_PUBLIC_URL ?? "http://localhost:3000"),
    issuer,
    internalIssuer: trimmed(process.env.OIDC_INTERNAL_ISSUER ?? issuer),
    clientId: process.env.OIDC_CLIENT_ID ?? "txrisk-workbench",
    apiUrl: trimmed(process.env.TXRISK_API_URL ?? "http://localhost:8000"),
    cookieKey: material,
  };
  return cached;
}

/** Cookies are `Secure` (and `__Host-` prefixed) whenever the workbench is served over HTTPS. */
export function isSecure(s: Settings = settings()): boolean {
  return s.publicUrl.startsWith("https://");
}

export function cookieName(base: string, s: Settings = settings()): string {
  return isSecure(s) ? `__Host-${base}` : base;
}
