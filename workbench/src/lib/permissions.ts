import type { components } from "./api-types";
import type { User } from "./oidc";

/**
 * What the workbench offers each role. It only decides what to show or disable: the API enforces
 * every one of these (403 for a role or an Analyst's Case it may not touch, 409 for a move the
 * Case's status does not allow), and the workbench shows its answer when it differs.
 */

export type CaseStatus = components["schemas"]["CaseOut"]["status"];
export type CaseSummary = components["schemas"]["CaseOut"];

export const STATUS_LABELS: Record<CaseStatus, string> = {
  open: "Open",
  investigating: "Investigating",
  closed_fraud: "Closed: fraud",
  closed_legitimate: "Closed: legitimate",
};

export function isClosed(status: CaseStatus): boolean {
  return status === "closed_fraud" || status === "closed_legitimate";
}

export function hasCaseScreens(user: User): boolean {
  return user.roles.includes("analyst") || user.roles.includes("supervisor");
}

export function isSupervisor(user: User): boolean {
  return user.roles.includes("supervisor");
}

/** A Supervisor works any open Case; an Analyst only a Case assigned to them. */
export function canWork(user: User, c: Pick<CaseSummary, "status" | "assignee_id">): boolean {
  if (isClosed(c.status)) {
    return false;
  }
  return isSupervisor(user) || (user.roles.includes("analyst") && c.assignee_id === user.sub);
}

export function canAssign(user: User, c: Pick<CaseSummary, "status">): boolean {
  return isSupervisor(user) && !isClosed(c.status);
}

export type Move = { to: "investigating" | "closed_fraud" | "closed_legitimate"; label: string };

const MOVES: Record<CaseStatus, Move[]> = {
  open: [{ to: "investigating", label: "Start investigating" }],
  investigating: [
    { to: "closed_fraud", label: "Close as fraud" },
    { to: "closed_legitimate", label: "Close as legitimate" },
  ],
  closed_fraud: [],
  closed_legitimate: [],
};

/** The moves the Case's status allows. A closed Case never reopens. */
export function nextMoves(status: CaseStatus): Move[] {
  return MOVES[status];
}

/** Why the person can't work the Case, for a message where the controls would be. */
export function whyNotWorkable(
  user: User,
  c: Pick<CaseSummary, "status" | "assignee_id" | "assignee_name">,
): string | null {
  if (isClosed(c.status)) {
    return "This Case is closed and cannot be changed.";
  }
  if (canWork(user, c)) {
    return null;
  }
  return c.assignee_id === null
    ? "This Case is unassigned. A Supervisor assigns it to an Analyst before notes or moves can be recorded."
    : `This Case is assigned to ${c.assignee_name ?? "another Analyst"}. Only the assigned Analyst or a Supervisor can work it.`;
}
