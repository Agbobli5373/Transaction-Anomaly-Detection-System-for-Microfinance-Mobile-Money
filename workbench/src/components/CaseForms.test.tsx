// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// The actions call the API from the server; the forms are what is under test.
vi.mock("@/app/cases/actions", () => ({
  addNote: vi.fn(),
  moveCase: vi.fn(),
  assignCase: vi.fn(),
  draftReport: vi.fn(),
  approveReport: vi.fn(),
}));

import { ApproveReportForm, MoveForm, NoteForm, ReportDraftForm } from "./CaseForms";
import { nextMoves } from "@/lib/permissions";

afterEach(cleanup);

const field = (label: string) => screen.getByLabelText(label) as HTMLTextAreaElement;
const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("the Case forms", () => {
  it("lets the person write a note when they can work the Case", () => {
    render(<NoteForm caseId={7} disabledReason={null} />);
    expect(field("Note").disabled).toBe(false);
    expect(button("Add note").disabled).toBe(false);
  });

  it("disables the note form when they cannot", () => {
    render(<NoteForm caseId={7} disabledReason="not yours" />);
    expect(field("Note").disabled).toBe(true);
    expect(button("Add note").disabled).toBe(true);
  });

  it("offers only the moves the status allows", () => {
    render(<MoveForm caseId={7} moves={nextMoves("open")} disabledReason={null} />);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Start investigating"]);
    cleanup();
    render(<MoveForm caseId={7} moves={nextMoves("investigating")} disabledReason={null} />);
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Close as fraud",
      "Close as legitimate",
    ]);
  });

  it("offers no move on a closed Case", () => {
    const { container } = render(
      <MoveForm caseId={7} moves={nextMoves("closed_fraud")} disabledReason="closed" />,
    );
    expect(container.innerHTML).toBe("");
  });

  it("disables approval, with the reason, for a person who cannot approve", () => {
    render(<ApproveReportForm caseId={7} disabledReason="Only a Supervisor can approve a report." />);
    expect(button("Approve report").disabled).toBe(true);
    expect(screen.getByTestId("approve-blocked").textContent).toMatch(/Only a Supervisor/);
    cleanup();
    render(<ApproveReportForm caseId={7} disabledReason={null} />);
    expect(button("Approve report").disabled).toBe(false);
  });

  it("offers the current draft for rewriting", () => {
    render(<ReportDraftForm caseId={7} current="Takeover after a SIM swap." />);
    expect(field("Summary").value).toBe("Takeover after a SIM swap.");
    expect(screen.getByRole("heading", { name: "Rewrite the draft" })).toBeTruthy();
  });
});
