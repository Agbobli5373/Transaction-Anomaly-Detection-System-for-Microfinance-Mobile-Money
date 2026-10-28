import { describe, expect, it } from "vitest";

import type { Role, User } from "./oidc";
import { canAssign, canWork, hasCaseScreens, nextMoves, whyNotWorkable } from "./permissions";

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
