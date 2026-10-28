import type { Credentials, User } from "./oidc";

/**
 * Signed-in sessions, held in the Next.js server's memory. The browser's cookie holds only an
 * encrypted session id; the credentials never leave the server, so none can reach browser
 * JavaScript, localStorage or the page. A restart signs everyone out (they sign in again,
 * silently if Keycloak's own session is alive). One replica of the workbench is assumed; to scale
 * out, put this store in Redis behind the same functions.
 */

export type SessionRecord = {
  user: User;
  credentials: Credentials;
  /** Epoch milliseconds: the session ends then, whatever the refresh grant allows. */
  endsAt: number;
  /** Set once the credentials could not be renewed: the person must sign in again. */
  expired: boolean;
  /** A refresh in flight, so concurrent requests share one. */
  refreshing?: Promise<void>;
};

export const SESSION_MAX_SECONDS = 8 * 60 * 60;
// Renew an access JWT this long before it expires, so a request never carries a stale one.
export const REFRESH_MARGIN_MS = 30_000;

type Store = Map<string, SessionRecord>;

const globalStore = globalThis as typeof globalThis & { __workbenchSessions?: Store };

export function store(): Store {
  globalStore.__workbenchSessions ??= new Map();
  return globalStore.__workbenchSessions;
}

/** Forgets sessions past their limit, so abandoned ones do not pile up in memory. */
export function pruneEnded(sessions: Store, now: number = Date.now()): void {
  for (const [id, record] of sessions) {
    if (now >= record.endsAt) {
      sessions.delete(id);
    }
  }
}

export type Renew = (refresh: string, now: number) => Promise<Credentials>;

/**
 * The session's credentials, renewed first if the access JWT is about to expire. When they can't
 * be renewed (the refresh grant is expired, revoked or refused) the session is marked expired and
 * null is returned; the caller signs the person out, including at Keycloak.
 */
export async function freshCredentials(
  record: SessionRecord,
  renew: Renew,
  now: number = Date.now(),
): Promise<Credentials | null> {
  if (record.expired) {
    return null;
  }
  if (now >= record.endsAt) {
    record.expired = true;
    return null;
  }
  if (now < record.credentials.accessExpiresAt - REFRESH_MARGIN_MS) {
    return record.credentials;
  }
  record.refreshing ??= (async () => {
    try {
      if (now >= record.credentials.refreshExpiresAt) {
        throw new Error("the refresh grant has expired");
      }
      record.credentials = await renew(record.credentials.refresh, now);
    } catch {
      record.expired = true;
    } finally {
      record.refreshing = undefined;
    }
  })();
  await record.refreshing;
  return record.expired ? null : record.credentials;
}
