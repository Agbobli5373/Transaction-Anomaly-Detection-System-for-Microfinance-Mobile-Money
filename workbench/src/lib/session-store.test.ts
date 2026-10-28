import { describe, expect, it, vi } from "vitest";

import type { Credentials } from "./oidc";
import {
  REFRESH_MARGIN_MS,
  freshCredentials,
  pruneEnded,
  type SessionRecord,
} from "./session-store";

const NOW = 1_900_000_000_000;

function credentials(label: string, accessLeftMs: number, refreshLeftMs = 600_000): Credentials {
  return {
    access: `${label}-access`,
    refresh: `${label}-refresh`,
    identity: `${label}-identity`,
    accessExpiresAt: NOW + accessLeftMs,
    refreshExpiresAt: NOW + refreshLeftMs,
  };
}

function session(c: Credentials): SessionRecord {
  return {
    user: { sub: "sub-ama", name: "Ama", roles: ["analyst"] },
    credentials: c,
    endsAt: NOW + 3_600_000,
    expired: false,
  };
}

describe("renewing a session's credentials", () => {
  it("keeps the access credential while it has time left", async () => {
    const renew = vi.fn();
    const record = session(credentials("one", 5 * 60_000));
    expect(await freshCredentials(record, renew, NOW)).toBe(record.credentials);
    expect(renew).not.toHaveBeenCalled();
  });

  it("renews it shortly before it expires, and keeps the new ones", async () => {
    const renewed = credentials("two", 5 * 60_000);
    const renew = vi.fn().mockResolvedValue(renewed);
    const record = session(credentials("one", REFRESH_MARGIN_MS - 1));
    expect(await freshCredentials(record, renew, NOW)).toBe(renewed);
    expect(renew).toHaveBeenCalledWith("one-refresh", NOW);
    expect(record.credentials).toBe(renewed);
  });

  it("makes concurrent requests share one renewal", async () => {
    const renewed = credentials("two", 5 * 60_000);
    const renew = vi.fn().mockResolvedValue(renewed);
    const record = session(credentials("one", -1));
    const answers = await Promise.all([
      freshCredentials(record, renew, NOW),
      freshCredentials(record, renew, NOW),
      freshCredentials(record, renew, NOW),
    ]);
    expect(renew).toHaveBeenCalledTimes(1);
    expect(answers).toEqual([renewed, renewed, renewed]);
  });

  it("ends the session when the identity provider refuses the renewal", async () => {
    const renew = vi.fn().mockRejectedValue(new Error("refused (400)"));
    const record = session(credentials("one", -1));
    expect(await freshCredentials(record, renew, NOW)).toBeNull();
    expect(record.expired).toBe(true);
    // And it stays ended: no further attempts with a refused grant.
    expect(await freshCredentials(record, renew, NOW)).toBeNull();
    expect(renew).toHaveBeenCalledTimes(1);
  });

  it("ends the session without asking when the refresh grant has expired", async () => {
    const renew = vi.fn();
    const record = session(credentials("one", -1, -1));
    expect(await freshCredentials(record, renew, NOW)).toBeNull();
    expect(renew).not.toHaveBeenCalled();
    expect(record.expired).toBe(true);
  });

  it("ends the session at its absolute limit", async () => {
    const record = session(credentials("one", 5 * 60_000));
    expect(await freshCredentials(record, vi.fn(), record.endsAt)).toBeNull();
    expect(record.expired).toBe(true);
  });
});

describe("abandoned sessions", () => {
  it("are forgotten once past their limit, and live ones are kept", () => {
    const live = session(credentials("live", 60_000));
    const stale = { ...session(credentials("stale", 60_000)), endsAt: NOW - 1 };
    const sessions = new Map([
      ["live", live],
      ["stale", stale],
    ]);
    pruneEnded(sessions, NOW);
    expect([...sessions.keys()]).toEqual(["live"]);
  });
});
