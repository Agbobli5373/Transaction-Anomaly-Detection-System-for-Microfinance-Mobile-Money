import { EncryptJWT, jwtDecrypt } from "jose";

/**
 * Encrypted, authenticated, expiring cookie values (a JWE: direct A256GCM). The browser holds
 * nothing it can read: the sign-in transaction and the session id are sealed with AUTH_SECRET.
 * A purpose is mixed into the key so a value sealed for one cookie never opens as another.
 */

async function keyFor(material: string, purpose: string): Promise<Uint8Array> {
  const bytes = new TextEncoder().encode(`txrisk-workbench:${purpose}:${material}`);
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
}

export async function seal(
  value: Record<string, string>,
  material: string,
  purpose: string,
  maxAgeSeconds: number,
): Promise<string> {
  return new EncryptJWT(value)
    .setProtectedHeader({ alg: "dir", enc: "A256GCM" })
    .setIssuedAt()
    .setExpirationTime(`${maxAgeSeconds}s`)
    .encrypt(await keyFor(material, purpose));
}

/** The sealed value, or null if it is forged, expired, sealed for another purpose or garbled. */
export async function unseal(
  sealed: string,
  material: string,
  purpose: string,
): Promise<Record<string, string> | null> {
  try {
    const { payload } = await jwtDecrypt(sealed, await keyFor(material, purpose), {
      keyManagementAlgorithms: ["dir"],
      contentEncryptionAlgorithms: ["A256GCM"],
    });
    const value: Record<string, string> = {};
    for (const [k, v] of Object.entries(payload)) {
      if (typeof v === "string") {
        value[k] = v;
      }
    }
    return value;
  } catch {
    return null;
  }
}
