import { expect, test } from "@playwright/test";

import { expectAccessible, seeded, stateFile } from "./support";

test.describe("an Analyst", () => {
  test.use({ storageState: stateFile("analyst") });

  test("has no assignment control, and the API's 403 is shown if the note form is forced", async ({
    page,
  }) => {
    const unassigned = seeded().cases.unassigned![0]!;
    await page.goto(`/cases/${unassigned.case_id}`);
    await expect(page.getByTestId("assignee")).toHaveText("Unassigned");

    // Nothing to assign with, and the Case explains why notes and moves are off.
    await expect(page.getByRole("heading", { name: /assign this case/i })).toHaveCount(0);
    await expect(page.getByLabel("Analyst ID")).toHaveCount(0);
    await expect(page.getByTestId("work-blocked")).toContainText("unassigned");
    const note = page.getByRole("textbox", { name: "Note" });
    await expect(note).toBeDisabled();
    await expect(page.getByRole("button", { name: "Add note" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Start investigating" })).toBeDisabled();
    await expectAccessible(page);

    // Forcing the form open sends it anyway; the API refuses and the workbench says what it said.
    await page.evaluate(() => {
      for (const el of document.querySelectorAll("textarea, button")) {
        el.removeAttribute("disabled");
      }
    });
    await note.fill("A note on a Case that is not mine.");
    await page.getByRole("button", { name: "Add note" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Not permitted" })).toBeVisible();
    await expect(page.getByTestId("history")).toHaveCount(0);

    // And the same for a move.
    await page.evaluate(() => {
      for (const el of document.querySelectorAll("textarea, button")) {
        el.removeAttribute("disabled");
      }
    });
    await page.getByRole("button", { name: "Start investigating" }).click();
    await expect(page.getByRole("alert").filter({ hasText: "Not permitted" })).toHaveCount(2);
    await expect(page.getByTestId("assignee")).toHaveText("Unassigned");
  });

  test("has no Supervisor-only controls on the queue either", async ({ page }) => {
    await page.goto("/cases");
    await expect(page.getByText("Cases assigned to you and the unassigned open ones")).toBeVisible();
  });
});

test.describe("a Supervisor", () => {
  test.use({ storageState: stateFile("supervisor") });

  test("sees every Case, including another Analyst's", async ({ page }) => {
    const s = seeded();
    await page.goto("/cases");
    await expect(page.getByText("All Cases, newest first.")).toBeVisible();
    await page.goto(`/cases?wallet=${s.cases.other![0]!.wallet_id}`);
    await expect(page.getByRole("link", { name: `Case ${s.cases.other![0]!.case_id}` })).toBeVisible();
    await expect(page.getByRole("cell", { name: "Another Analyst" })).toBeVisible();
  });
});
