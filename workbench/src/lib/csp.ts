/** The page Content-Security-Policy (set per request, with its nonce, by src/proxy.ts). */

export function contentSecurityPolicy(nonce: string, keycloakOrigin: string, dev: boolean): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(dev ? ["'unsafe-eval'"] : [])],
    // Next.js ships its CSS as files; the dev server injects inline styles.
    "style-src": ["'self'", ...(dev ? ["'unsafe-inline'"] : [])],
    "img-src": ["'self'", "data:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'"],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "frame-ancestors": ["'none'"],
    // Signing out is a form post that the server answers by redirecting to Keycloak's end-session.
    "form-action": ["'self'", keycloakOrigin],
  };
  return Object.entries(directives)
    .map(([name, sources]) => `${name} ${sources.join(" ")}`)
    .join("; ");
}
