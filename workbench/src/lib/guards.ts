import { redirect } from "next/navigation";

import type { User } from "./oidc";
import { hasCaseScreens } from "./permissions";
import { requireSession } from "./session";

/** For the Case screens and the watch list: Analysts and Supervisors only (Admins have none). */
export async function requireCaseUser(): Promise<User> {
  const { user } = await requireSession();
  if (!hasCaseScreens(user)) {
    redirect("/no-access");
  }
  return user;
}

/** A query-string value, whichever of string, list or missing it arrives as. */
export function single(value: string | string[] | undefined): string | undefined {
  const one = Array.isArray(value) ? value[0] : value;
  return one && one.trim() ? one.trim() : undefined;
}

export function withParams(path: string, params: Record<string, string | undefined>): string {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (value) {
      query.set(name, value);
    }
  }
  const text = query.toString();
  return text ? `${path}?${text}` : path;
}
