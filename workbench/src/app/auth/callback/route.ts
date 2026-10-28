import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { settings } from "@/lib/config";
import { exchangeCode, userFrom } from "@/lib/oidc";
import { safeNext } from "@/lib/redirects";
import { unseal } from "@/lib/seal";
import {
  SIGN_IN_PURPOSE,
  cookieOptions,
  sessionCookieName,
  signInCookieName,
  startSession,
} from "@/lib/session";
import { SESSION_MAX_SECONDS } from "@/lib/session-store";

export const dynamic = "force-dynamic";

function finish(path: string): NextResponse {
  const response = NextResponse.redirect(new URL(path, settings().publicUrl));
  response.cookies.delete({ name: signInCookieName(), path: "/auth" });
  return response;
}

/** Finishes sign-in: checks state, redeems the code with the PKCE verifier, opens the session. */
export async function GET(request: Request) {
  const s = settings();
  const params = new URL(request.url).searchParams;
  const code = params.get("code");
  const sealed = (await cookies()).get(signInCookieName())?.value;
  const transaction = sealed ? await unseal(sealed, s.cookieKey, SIGN_IN_PURPOSE) : null;

  if (!transaction || !code || params.get("state") !== transaction.state) {
    return finish("/signin-failed");
  }
  try {
    const credentials = await exchangeCode(s, code, transaction.verifier ?? "");
    const user = await userFrom(s, credentials, transaction.nonce ?? "");
    const response = finish(safeNext(transaction.next ?? null));
    response.cookies.set(
      sessionCookieName(),
      await startSession(user, credentials),
      cookieOptions(SESSION_MAX_SECONDS),
    );
    return response;
  } catch (error) {
    // Only the reason, never the grant or the code.
    console.error("sign-in failed:", error instanceof Error ? error.message : "unknown error");
    return finish("/signin-failed");
  }
}
