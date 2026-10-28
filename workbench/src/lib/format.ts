/** Display helpers: no raw phone numbers, and dates in UTC so a page reads the same everywhere. */

// Nine to fifteen digits standing alone (optionally with a leading +, or in groups of three or
// four split by spaces or dashes) look like an MSISDN. The API pseudonymises Wallets, so none should arrive; free text an Analyst
// types might hold one, and it is masked on display.
const PHONE_SHAPE = /(?<![\w.:-])\+?(?:\d{9,15}|\d{2,4}(?:[ -]\d{3,4}){2,3})(?![\w:-]|\.\d)/g;

export function maskPhoneNumbers(text: string): string {
  return text.replace(PHONE_SHAPE, (match) => {
    const digits = match.replace(/\D/g, "");
    return `${"•".repeat(Math.max(digits.length - 3, 0))}${digits.slice(-3)}`;
  });
}

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : `${DATE_TIME.format(date)} UTC`;
}

export function formatAmount(amount: number): string {
  return `GHS ${amount.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "n/a" : score.toFixed(1);
}

export function formatReasonValue(value: number | string | null | undefined): string {
  if (value === null || value === undefined) {
    return "none";
  }
  return typeof value === "number" ? String(Math.round(value * 1000) / 1000) : value;
}

export function labelOf(code: string): string {
  const spaced = code.replace(/_/g, " ").toLowerCase();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
