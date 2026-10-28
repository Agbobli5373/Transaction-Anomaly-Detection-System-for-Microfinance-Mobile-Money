import { STATUS_LABELS, type CaseStatus } from "@/lib/permissions";

export function StatusBadge({ status }: { status: CaseStatus }) {
  return <span className={`badge status-${status}`}>{STATUS_LABELS[status]}</span>;
}

export function BandBadge({ band }: { band: "Low" | "Medium" | "High" }) {
  return <span className={`badge band-${band.toLowerCase()}`}>{band}</span>;
}
