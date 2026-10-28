import { describe, expect, it } from "vitest";

import { contentSecurityPolicy } from "./csp";

describe("the Content-Security-Policy", () => {
  const policy = contentSecurityPolicy("abc123", "http://localhost:8080", false);

  it("runs only scripts with the request's nonce, and never inline or eval", () => {
    expect(policy).toContain("script-src 'self' 'nonce-abc123' 'strict-dynamic'");
    expect(policy).not.toContain("unsafe-eval");
    expect(policy).not.toContain("unsafe-inline");
  });

  it("blocks framing, plugins, foreign bases and foreign connections", () => {
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("base-uri 'self'");
    expect(policy).toContain("connect-src 'self'");
  });

  it("lets a form go to this site and to Keycloak (sign-out redirects there)", () => {
    expect(policy).toContain("form-action 'self' http://localhost:8080");
  });

  it("relaxes only script evaluation and styles for the dev server", () => {
    const dev = contentSecurityPolicy("n", "http://localhost:8080", true);
    expect(dev).toContain("'unsafe-eval'");
  });
});
