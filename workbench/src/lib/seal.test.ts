import { describe, expect, it } from "vitest";

import { randomValue } from "./oidc";
import { safeNext } from "./redirects";
import { seal, unseal } from "./seal";

const material = randomValue();

describe("sealed cookie values", () => {
  it("opens with the same key and purpose", async () => {
    const sealed = await seal({ sid: "abc" }, material, "session", 60);
    expect(await unseal(sealed, material, "session")).toMatchObject({ sid: "abc" });
  });

  it("does not show its content", async () => {
    const sealed = await seal({ sid: "visible-text" }, material, "session", 60);
    expect(sealed).not.toContain("visible-text");
    expect(Buffer.from(sealed.split(".")[3] ?? "", "base64url").toString()).not.toContain(
      "visible-text",
    );
  });

  it("refuses another key, another purpose, an expired value and garbage", async () => {
    const sealed = await seal({ sid: "abc" }, material, "session", 60);
    expect(await unseal(sealed, randomValue(), "session")).toBeNull();
    expect(await unseal(sealed, material, "sign-in")).toBeNull();
    expect(await unseal(await seal({ sid: "abc" }, material, "session", -10), material, "session")).toBeNull();
    expect(await unseal("not.a.value", material, "session")).toBeNull();
  });

  it("refuses a value with one character changed", async () => {
    const sealed = await seal({ sid: "abc" }, material, "session", 60);
    // header..iv.ciphertext.tag: change the ciphertext's first character.
    const parts = sealed.split(".");
    const cipher = parts[3] ?? "";
    parts[3] = (cipher.startsWith("A") ? "B" : "A") + cipher.slice(1);
    expect(await unseal(parts.join("."), material, "session")).toBeNull();
  });
});

describe("where to go after signing in", () => {
  it("accepts a path on this site", () => {
    expect(safeNext("/cases/12?x=1")).toBe("/cases/12?x=1");
  });

  it("falls back to home for anything that could leave the site", () => {
    for (const next of [null, "", "https://evil.example/", "//evil.example", "/\\evil.example", "cases"]) {
      expect(safeNext(next)).toBe("/");
    }
  });
});
