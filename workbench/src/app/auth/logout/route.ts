import { NextResponse } from "next/server";

import { settings } from "@/lib/config";
import { endSessionUrl } from "@/lib/oidc";
import { currentSessionId, sessionCookieName } from "@/lib/session";
import { store } from "@/lib/session-store";

export const dynamic = "force-dynamic";

/** Ends the session here and at Keycloak (its end-session endpoint, which lands on /signed-out). */
async function signOut(landing: string): Promise<NextResponse> {
  const s = settings();
  const id = await currentSessionId();
  const record = id ? store().get(id) : undefined;
  if (id) {
    store().delete(id);
  }
  const target = record ? endSessionUrl(s, record.credentials.identity, landing) : null;
  const response = NextResponse.redirect(target ?? new URL(landing, s.publicUrl), 303);
  response.cookies.delete(sessionCookieName());
  return response;
}

/** The Sign out button: a POST from this site's own page. */
export async function POST(request: Request) {
  const origin = request.headers.get("origin");
  if (origin !== null && origin !== new URL(settings().publicUrl).origin) {
    return new NextResponse("cross-site sign-out refused", { status: 403 });
  }
  return signOut("/signed-out");
}

/** The workbench's own redirect when a session could not be renewed; any other GET just goes home. */
export async function GET(request: Request) {
  const reason = new URL(request.url).searchParams.get("reason");
  if (reason !== "expired") {
    return NextResponse.redirect(new URL("/", settings().publicUrl), 303);
  }
  return signOut("/signed-out?reason=expired");
}
