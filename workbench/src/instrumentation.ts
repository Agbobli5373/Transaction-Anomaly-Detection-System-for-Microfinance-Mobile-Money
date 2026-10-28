import { settings } from "@/lib/config";

/** The production server stops at start, not at the first request, if its settings are missing. */
export function register() {
  if (process.env.NEXT_RUNTIME === "nodejs" && process.env.NODE_ENV === "production") {
    settings();
  }
}
