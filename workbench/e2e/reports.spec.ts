import { readFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import { expectAccessible, seeded, stateFile } from "./support";

// A raw phone number, typed into the summary on purpose: it must not survive into the file.
const PHONE = "233241234567";

test("an Analyst drafts a report from a Case closed as fraud but cannot approve it; a Supervisor approves and exports it", async ({
  browser,
}) => {
  const target = seeded().cases.reports![0]!;

  // --- the Analyst closes the Case as fraud, then drafts ---
  const analystContext = await browser.newContext({ storageState: stateFile("analyst") });
  const analyst = await analystContext.newPage();
  await analyst.goto(`/cases/${target.case_id}`);
  // No report while the Case is not closed as fraud.
  await expect(analyst.getByRole("heading", { name: "Suspicious transaction report" })).toHaveCount(0);

  await analyst.getByRole("button", { name: "Start investigating" }).click();
  await analyst.getByLabel("Comment (optional)").fill("Owner confirmed the takeover.");
  await analyst.getByRole("button", { name: "Close as fraud" }).click();
  await expect(
    analyst.getByRole("heading", { name: "Suspicious transaction report" }),
  ).toBeVisible();
  await expect(analyst.getByText("No report has been drafted for this Case yet.")).toBeVisible();
  await expect(analyst.getByRole("button", { name: "Approve report" })).toHaveCount(0);

  await analyst
    .getByLabel("Summary")
    .fill(`SIM-swap takeover; the victim can be reached on ${PHONE}.`);
  await analyst.getByRole("button", { name: "Save draft" }).click();
  await expect(analyst.getByText("Report draft saved.")).toBeVisible();
  await expect(analyst.getByTestId("report-status")).toHaveText("Draft");
  // The number is masked on screen.
  await expect(analyst.getByTestId("report-summary")).not.toContainText(PHONE);
  await expect(analyst.getByTestId("report-summary")).toContainText("•••••••••567");
  await expectAccessible(analyst);

  // --- the Analyst cannot approve: the button is off, and the API refuses a forced request ---
  const approve = analyst.getByRole("button", { name: "Approve report" });
  await expect(approve).toBeDisabled();
  await expect(analyst.getByTestId("approve-blocked")).toContainText("Only a Supervisor");
  await analyst.evaluate(() => {
    for (const el of document.querySelectorAll("button")) {
      el.removeAttribute("disabled");
    }
  });
  await approve.click();
  await expect(analyst.getByRole("alert").filter({ hasText: "Not permitted" })).toBeVisible();
  await expect(analyst.getByTestId("report-status")).toHaveText("Draft");
  // Nor can the Analyst reach the file, even by its address.
  const refused = await analystContext.request.get(`/cases/${target.case_id}/report/export`);
  expect(refused.status()).toBe(403);

  // --- the Supervisor approves, then exports ---
  const supervisorContext = await browser.newContext({
    storageState: stateFile("supervisor"),
    acceptDownloads: true,
  });
  const supervisor = await supervisorContext.newPage();
  await supervisor.goto(`/cases/${target.case_id}`);
  await expect(supervisor.getByTestId("report-status")).toHaveText("Draft");
  // A draft cannot be exported.
  await expect(supervisor.getByRole("link", { name: "Export the report file" })).toHaveCount(0);
  const early = await supervisorContext.request.get(`/cases/${target.case_id}/report/export`);
  expect(early.status()).toBe(409);

  await supervisor.getByRole("button", { name: "Approve report" }).click();
  await expect(supervisor.getByText("Report approved.")).toBeVisible();
  await expect(supervisor.getByTestId("report-status")).toHaveText("Approved");
  await expectAccessible(supervisor);

  const [download] = await Promise.all([
    supervisor.waitForEvent("download"),
    supervisor.getByRole("link", { name: "Export the report file" }).click(),
  ]);
  expect(download.suggestedFilename()).toBe(
    `suspicious-transaction-report-case-${target.case_id}.json`,
  );
  const text = readFileSync((await download.path())!, "utf8");
  const file = JSON.parse(text);
  expect(file.case.case_id).toBe(target.case_id);
  expect(file.transactions.length).toBeGreaterThan(0);
  expect(file.transactions[0].assessment.reasons.length).toBeGreaterThan(0);
  expect(file.notes.map((n: { text: string }) => n.text)).toContain("Owner confirmed the takeover.");
  expect(file.approved_by).toBe("demo-supervisor");
  // No raw MSISDN anywhere in the file.
  expect(text).not.toContain(PHONE);
  expect(text).not.toMatch(/(?<![\w.:-])\+?\d{9,15}(?![\w:-])/);

  // --- the Analyst now sees it approved and final ---
  await analyst.reload();
  await expect(analyst.getByTestId("report-status")).toHaveText("Approved");
  await expect(analyst.getByLabel("Summary")).toHaveCount(0);
  await expect(analyst.getByRole("link", { name: "Export the report file" })).toHaveCount(0);

  await analystContext.close();
  await supervisorContext.close();
});
