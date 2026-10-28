import { expect, test } from "@playwright/test";

import { expectAccessible, seeded, stateFile } from "./support";

test("a Supervisor assigns a Case to an Analyst, and the Analyst sees it as theirs", async ({
  browser,
}) => {
  const { analystId, cases } = seeded();
  const target = cases.supervisor![0]!;

  const analystContext = await browser.newContext({ storageState: stateFile("analyst") });
  const analyst = await analystContext.newPage();
  await analyst.goto("/cases?assignee=me");
  await expect(analyst.getByRole("heading", { name: "Cases", level: 1 })).toBeVisible();
  await expect(analyst.getByRole("link", { name: `Case ${target.case_id}` })).toHaveCount(0);

  const supervisorContext = await browser.newContext({ storageState: stateFile("supervisor") });
  const supervisor = await supervisorContext.newPage();
  await supervisor.goto(`/cases/${target.case_id}`);
  await expect(supervisor.getByTestId("assignee")).toHaveText("Unassigned");
  await expect(supervisor.getByRole("heading", { name: "Assign this Case" })).toBeVisible();
  await expectAccessible(supervisor);

  await supervisor.getByLabel("Analyst ID").fill(analystId);
  await supervisor.getByLabel("Analyst name").fill("Demo Analyst");
  await supervisor.getByRole("button", { name: "Assign" }).click();
  await expect(supervisor.getByText("Case assigned.")).toBeVisible();
  await expect(supervisor.getByTestId("assignee")).toHaveText("Demo Analyst");
  await expect(supervisor.getByTestId("history")).toContainText("demo-supervisor assigned the Case");

  // The Supervisor may also move the Case on, as any Case's owner would.
  await supervisor.getByRole("button", { name: "Start investigating" }).click();
  await expect(supervisor.getByRole("button", { name: "Close as fraud" })).toBeVisible();

  // The Analyst now has it among their own, and may work it.
  await analyst.goto("/cases?assignee=me");
  await expect(analyst.getByRole("link", { name: `Case ${target.case_id}` })).toBeVisible();
  await analyst.getByRole("link", { name: `Case ${target.case_id}` }).click();
  await expect(analyst.getByRole("textbox", { name: "Note" })).toBeEnabled();
  await expect(analyst.getByRole("button", { name: "Close as fraud" })).toBeEnabled();

  await analystContext.close();
  await supervisorContext.close();
});

test("a Supervisor closes any Case as fraud", async ({ browser }) => {
  const target = seeded().cases.supervisor![1]!;
  const context = await browser.newContext({ storageState: stateFile("supervisor") });
  const page = await context.newPage();
  await page.goto(`/cases/${target.case_id}`);
  await page.getByRole("button", { name: "Start investigating" }).click();
  await page.getByRole("button", { name: "Close as fraud" }).click();
  await expect(page.getByText("Closed: fraud").first()).toBeVisible();
  await expect(page.getByText("Confirmed fraud")).toBeVisible();
  await context.close();
});
