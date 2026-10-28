import { describe, expect, it } from "vitest";

import type { Role, User } from "./oidc";
import {
  canAssign,
  canDraftReport,
  canExportReport,
  canWork,
  hasCaseScreens,
  nextMoves,
  whyNotApprovable,
  whyNotWorkable,
} from "./permissions";

const person = (...roles: Role[]): User => ({ sub: "sub-ama", name: "Ama", roles });
const analyst = person("analyst");
const supervisor = person("supervisor");
const admin = person("admin");

const mine = { status: "investigating", assignee_id: "sub-ama", assignee_name: "Ama" } as const;
const theirs = { status: "open", assignee_id: "sub-yaw", assignee_name: "Yaw" } as const;
const unassigned = { status: "open", assignee_id: null, assignee_name: null } as const;
const closed = { status: "closed_fraud", assignee_id: "sub-ama", assignee_name: "Ama" } as const;

describe("what each role is offered", () => {
  it("gives Analysts and Supervisors the Case screens, and Admins none", () => {
    expect(hasCaseScreens(analyst)).toBe(true);
    expect(hasCaseScreens(supervisor)).toBe(true);
    expect(hasCaseScreens(admin)).toBe(false);
  });

  it("lets an Analyst work only a Case assigned to them", () => {
    expect(canWork(analyst, mine)).toBe(true);
    expect(canWork(analyst, theirs)).toBe(false);
    expect(canWork(analyst, unassigned)).toBe(false);
  });

  it("lets a Supervisor work any open Case, and nobody a closed one", () => {
    expect(canWork(supervisor, theirs)).toBe(true);
    expect(canWork(supervisor, closed)).toBe(false);
    expect(canWork(analyst, closed)).toBe(false);
  });

  it("offers assignment to Supervisors only, and not on a closed Case", () => {
    expect(canAssign(supervisor, theirs)).toBe(true);
    expect(canAssign(analyst, theirs)).toBe(false);
    expect(canAssign(supervisor, closed)).toBe(false);
  });

  it("explains why a Case can't be worked", () => {
    expect(whyNotWorkable(analyst, mine)).toBeNull();
    expect(whyNotWorkable(analyst, unassigned)).toMatch(/unassigned/);
    expect(whyNotWorkable(analyst, theirs)).toMatch(/assigned to Yaw/);
    expect(whyNotWorkable(supervisor, closed)).toMatch(/closed/);
  });
});

describe("the moves a status allows", () => {
  it("goes open, investigating, then closed", () => {
    expect(nextMoves("open").map((m) => m.to)).toEqual(["investigating"]);
    expect(nextMoves("investigating").map((m) => m.to)).toEqual([
      "closed_fraud",
      "closed_legitimate",
    ]);
  });

  it("never reopens a closed Case", () => {
    expect(nextMoves("closed_fraud")).toEqual([]);
    expect(nextMoves("closed_legitimate")).toEqual([]);
  });
});

describe("Suspicious transaction reports", () => {
  const draft = { status: "draft" } as const;
  const approved = { status: "approved" } as const;
  const legit = { status: "closed_legitimate", assignee_id: "sub-ama" } as const;

  it("is drafted only from a Case closed as fraud, by its Analyst or a Supervisor", () => {
    expect(canDraftReport(analyst, closed, null)).toBe(true);
    expect(canDraftReport(analyst, closed, draft)).toBe(true);
    expect(canDraftReport(supervisor, closed, null)).toBe(true);
    expect(canDraftReport(person("analyst"), { ...closed, assignee_id: "sub-yaw" }, null)).toBe(false);
    expect(canDraftReport(admin, closed, null)).toBe(false);
    expect(canDraftReport(supervisor, legit, null)).toBe(false);
    expect(canDraftReport(supervisor, mine, null)).toBe(false);
  });

  it("is final once approved", () => {
    expect(canDraftReport(supervisor, closed, approved)).toBe(false);
  });

  it("is approved by a Supervisor only, and says why an Analyst cannot", () => {
    expect(whyNotApprovable(supervisor, draft)).toBeNull();
    expect(whyNotApprovable(analyst, draft)).toMatch(/Only a Supervisor/);
    expect(whyNotApprovable(supervisor, approved)).toMatch(/approved/);
  });

  it("is exported by a Supervisor, and only once approved", () => {
    expect(canExportReport(supervisor, approved)).toBe(true);
    expect(canExportReport(supervisor, draft)).toBe(false);
    expect(canExportReport(supervisor, null)).toBe(false);
    expect(canExportReport(analyst, approved)).toBe(false);
  });
});
