import { expect, test } from "@playwright/test";

import { expectAccessible, seeded, stateFile } from "./support";

test.use({ storageState: stateFile("analyst") });

test("an Analyst opens a Case from the queue, adds a note and closes it", async ({ page }) => {
  const [mine] = seeded().cases.analyst!;

  // The queue: filter by Wallet, then open the Case.
  await page.goto("/cases");
  await expect(page.getByRole("heading", { name: "Cases", level: 1 })).toBeVisible();
  await expectAccessible(page);
  await page.getByLabel("Wallet").fill(mine!.wallet_id);
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(page.getByRole("row")).toHaveCount(2); // the header and the one Case
  await page.getByRole("link", { name: `Case ${mine!.case_id}` }).click();

  // The Case view: the Alert's Risk score and Reasons with their values, recent Transactions.
  await expect(page.getByRole("heading", { name: `Case ${mine!.case_id}`, level: 1 })).toBeVisible();
  await expect(page.getByTestId("risk-score")).toHaveText(/^\d+\.\d$/);
  await expect(page.getByText("Balance drained shortly after a SIM swap")).toBeVisible();
  await expect(page.getByTestId("reason-value").first()).toHaveText("0.9");
  await expect(page.getByText("Amount well above the Wallet's usual")).toBeVisible();
  const transactions = page.getByRole("region", { name: "Recent Transactions" }).getByRole("row");
  await expect(transactions).toHaveCount(6); // the header, the High one and four routine ones
  await expect(page.getByTestId("assignee")).toHaveText("Demo Analyst");
  // No assignment control for an Analyst.
  await expect(page.getByRole("heading", { name: /assign this case/i })).toHaveCount(0);
  await expectAccessible(page);

  // A note.
  await page.getByRole("textbox", { name: "Note" }).fill("Called the wallet owner; no answer. Reach 233241234567 later.");
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(page.getByText("Note added.")).toBeVisible();
  const history = page.getByTestId("history");
  await expect(history).toContainText("demo-analyst added a note");
  await expect(history).toContainText("Called the wallet owner; no answer.");
  // A phone number in a note is masked on screen.
  await expect(history).not.toContainText("233241234567");
  await expect(history).toContainText("•••••••••567");

  // Open, then investigating, then closed: only valid next states are offered.
  await expect(page.getByRole("button", { name: "Close as fraud" })).toHaveCount(0);
  await page.getByRole("button", { name: "Start investigating" }).click();
  await expect(page.getByRole("button", { name: "Close as legitimate" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start investigating" })).toHaveCount(0);
  await page.getByLabel("Comment (optional)").fill("Owner confirmed the payment.");
  await page.getByRole("button", { name: "Close as legitimate" }).click();
  await expect(page.getByText("Closed: legitimate").first()).toBeVisible();
  await expect(history).toContainText("moved the Case from Investigating to Closed: legitimate");
  await expect(history).toContainText("Owner confirmed the payment.");
  // A closed Case offers nothing to do.
  await expect(page.getByRole("button", { name: "Add note" })).toHaveCount(0);
  await expect(page.getByText("This Case is closed and cannot be changed.")).toBeVisible();
  await expectAccessible(page);

  // The queue shows it closed.
  await page.goto(`/cases?wallet=${mine!.wallet_id}&status=closed_legitimate`);
  await expect(page.getByRole("link", { name: `Case ${mine!.case_id}` })).toBeVisible();
});

test("the API's 409 is shown when a Case changed under the Analyst", async ({ page, browser }) => {
  const second = seeded().cases.analyst![1]!;
  await page.goto(`/cases/${second.case_id}`);
  const stale = page.getByRole("button", { name: "Start investigating" });
  await expect(stale).toBeVisible();

  // Meanwhile the same Analyst moves it in another window.
  const other = await browser.newContext({ storageState: stateFile("analyst") });
  const otherPage = await other.newPage();
  await otherPage.goto(`/cases/${second.case_id}`);
  await otherPage.getByRole("button", { name: "Start investigating" }).click();
  await expect(otherPage.getByRole("button", { name: "Close as fraud" })).toBeVisible();
  await other.close();

  await stale.click();
  await expect(page.getByRole("alert").filter({ hasText: "Not possible now" })).toBeVisible();
});

test("an Analyst sees their own Cases and the unassigned open ones, never another Analyst's", async ({
  page,
}) => {
  const s = seeded();
  await page.goto("/cases?assignee=me");
  await expect(page.getByRole("link", { name: `Case ${s.cases.analyst![2]!.case_id}` })).toBeVisible();
  await expect(page.getByRole("link", { name: `Case ${s.cases.unassigned![0]!.case_id}` })).toHaveCount(0);

  await page.goto("/cases");
  await expect(page.getByRole("link", { name: `Case ${s.cases.unassigned![0]!.case_id}` })).toBeVisible();
  await expect(page.getByRole("link", { name: `Case ${s.cases.other![0]!.case_id}` })).toHaveCount(0);

  // Opening another Analyst's Case directly: the API refuses, and the page says so.
  await page.goto(`/cases/${s.cases.other![0]!.case_id}`);
  await expect(page.getByRole("alert").filter({ hasText: "Not permitted" })).toBeVisible();
});

test("the queue and a Case read well on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 700 });
  const first = seeded().cases.analyst![3]!;
  for (const path of ["/cases", `/cases/${first.case_id}`]) {
    await page.goto(path);
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  }
  await expectAccessible(page);
});
