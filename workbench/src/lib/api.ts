import createClient from "openapi-fetch";
import { redirect, unstable_rethrow } from "next/navigation";

import type { components, paths } from "./api-types";
import { settings } from "./config";
import { requireSession } from "./session";

/**
 * The txrisk API, called from the Next.js server only, as the signed-in person: their access JWT
 * is added here and goes no further than the API. The types come from contracts/openapi.json
 * (`npm run api-types`; CI checks they match).
 */

export type Schemas = components["schemas"];

export type Outcome<T> =
  | { ok: true; data: T }
  | { ok: false; status: number; message: string };

const SCHEME = "Bearer";

export async function api(): Promise<{ client: ReturnType<typeof createClient<paths>> }> {
  const { access } = await requireSession();
  const client = createClient<paths>({
    baseUrl: settings().apiUrl,
    headers: { Authorization: `${SCHEME} ${access}` },
    // The API is live data, and a person's view of it is theirs alone.
    cache: "no-store",
  });
  return { client };
}

/** What the API said went wrong, in words for the person. */
export function explain(status: number, detail: unknown): string {
  const said = typeof detail === "string" ? detail : undefined;
  switch (status) {
    case 401:
      return "Your sign-in has ended. Sign in again.";
    case 403:
      return said && said !== "Forbidden"
        ? `Not permitted: ${said}.`
        : "Not permitted: your role cannot do this.";
    case 404:
      return "That does not exist, or you cannot see it.";
    case 409:
      return said ? `Not possible now: ${said}.` : "Not possible now: the Case has changed.";
    case 422:
      return typeof detail === "string" ? detail : "The API did not accept that input.";
    default:
      return "The API could not be reached or failed. Try again shortly.";
  }
}

/** Unpacks an openapi-fetch result; a 401 means the sign-in ended, so leave for sign-out. */
export function outcome<T>(result: {
  data?: T;
  error?: unknown;
  response: Response;
}): Outcome<T> {
  if (result.response.ok && result.data !== undefined) {
    return { ok: true, data: result.data };
  }
  if (result.response.status === 401) {
    redirect("/auth/logout?reason=expired");
  }
  const detail = (result.error as { detail?: unknown } | undefined)?.detail;
  return {
    ok: false,
    status: result.response.status,
    message: explain(result.response.status, detail),
  };
}

/** Runs an API call, turning a network failure into a 502-shaped outcome. */
export async function attempt<T>(
  call: () => Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<Outcome<T>> {
  try {
    return outcome(await call());
  } catch (error) {
    unstable_rethrow(error);
    return { ok: false, status: 502, message: explain(502, undefined) };
  }
}
