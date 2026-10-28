import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { cookieName, isSecure, settings } from "./config";
import { refreshCredentials, type Credentials, type Role, type User } from "./oidc";
import { seal, unseal } from "./seal";
import {
  SESSION_MAX_SECONDS,
  freshCredentials,
  pruneEnded,
  store,
  type SessionRecord,
} from "./session-store";

const SESSION = "txw_session";
const SIGN_IN = "txw_oidc";

export const SESSION_PURPOSE = "session";
export const SIGN_IN_PURPOSE = "sign-in";

export function sessionCookieName(): string {
  return cookieName(SESSION);
}

export function signInCookieName(): string {
  return cookieName(SIGN_IN);
}

/** The attributes of every cookie the workbench sets: httpOnly, SameSite=Lax, Secure over HTTPS. */
export function cookieOptions(maxAgeSeconds: number, path = "/") {
  return {
    httpOnly: true,
    secure: isSecure(),
    sameSite: "lax" as const,
    path,
    maxAge: maxAgeSeconds,
  };
}

export async function startSession(user: User, credentials: Credentials): Promise<string> {
  const id = crypto.randomUUID();
  pruneEnded(store());
  store().set(id, {
    user,
    credentials,
    endsAt: Date.now() + SESSION_MAX_SECONDS * 1000,
    expired: false,
  });
  return seal({ sid: id }, settings().cookieKey, SESSION_PURPOSE, SESSION_MAX_SECONDS);
}

export async function sessionIdOf(value: string | undefined): Promise<string | null> {
  if (!value) {
    return null;
  }
  const opened = await unseal(value, settings().cookieKey, SESSION_PURPOSE);
  return opened?.sid ?? null;
}

export async function currentSessionId(): Promise<string | null> {
  return sessionIdOf((await cookies()).get(sessionCookieName())?.value);
}

export type Current =
  | { state: "signed-out" }
  | { state: "expired" }
  | { state: "active"; user: User; access: string };

/** The signed-in person and a current access JWT for calling the API (renewed when needed). */
export async function current(): Promise<Current> {
  const id = await currentSessionId();
  const record: SessionRecord | undefined = id ? store().get(id) : undefined;
  if (!record) {
    return { state: "signed-out" };
  }
  const credentials = await freshCredentials(record, (refresh, now) =>
    refreshCredentials(settings(), refresh, fetch, now),
  );
  if (!credentials) {
    return { state: "expired" };
  }
  return { state: "active", user: record.user, access: credentials.access };
}

/** For pages and actions that need a signed-in person: otherwise leave for sign-in or sign-out. */
export async function requireSession(): Promise<{ user: User; access: string }> {
  const now = await current();
  if (now.state === "active") {
    return now;
  }
  redirect(now.state === "expired" ? "/auth/logout?reason=expired" : "/");
}

export function hasRole(user: User, role: Role): boolean {
  return user.roles.includes(role);
}
