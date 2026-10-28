import { NextResponse } from "next/server";

import { settings } from "@/lib/config";
import { authorizationUrl, randomValue } from "@/lib/oidc";
import { safeNext } from "@/lib/redirects";
import { seal } from "@/lib/seal";
import { SIGN_IN_PURPOSE, cookieOptions, signInCookieName } from "@/lib/session";

export const dynamic = "force-dynamic";

const SIGN_IN_SECONDS = 10 * 60;

/** Starts sign-in: a PKCE challenge, a state and a nonce, kept in a short-lived sealed cookie. */
export async function GET(request: Request) {
  const s = settings();
  const transaction = {
    state: randomValue(),
    nonce: randomValue(),
    verifier: randomValue(48),
  };
  const next = safeNext(new URL(request.url).searchParams.get("next"));
  const response = NextResponse.redirect(await authorizationUrl(s, transaction));
  response.cookies.set(
    signInCookieName(),
    await seal({ ...transaction, next }, s.cookieKey, SIGN_IN_PURPOSE, SIGN_IN_SECONDS),
    cookieOptions(SIGN_IN_SECONDS, "/auth"),
  );
  return response;
}
