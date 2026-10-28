import { expect, test } from "@playwright/test";

import { expectAccessible, seeded, stateFile } from "./support";

test.use({ storageState: stateFile("analyst") });

// 30 seeded Medium Transactions, newest first, 25 to a page; they cycle through four regions,
// three Transaction types and three Wallets.
test("the Medium watch list filters by Wallet, region and Transaction type, and pages by cursor", async ({
  page,
}) => {
  const watched = (n: number) => `w-e2ew${seeded().suffix}-m${n}`;
  await page.goto("/watchlist");
  await expect(page.getByRole("heading", { name: "Medium watch list", level: 1 })).toBeVisible();
  await expectAccessible(page);

  const rows = page.getByRole("region", { name: "Medium watch list" }).getByRole("row");
  await expect(rows).toHaveCount(26); // the header and a page of 25

  // The next page holds the other five.
  await page.getByRole("link", { name: "Older Transactions" }).click();
  await expect(rows).toHaveCount(6);
  await expect(page.getByRole("link", { name: "Older Transactions" })).toHaveCount(0);
  await page.getByRole("link", { name: "Back to the newest" }).click();
  await expect(rows).toHaveCount(26);

  // Wallet: one of three.
  await page.getByLabel("Wallet").fill(watched(0));
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(rows).toHaveCount(11); // m = 0, 3, ..., 27
  await expect(page.getByRole("cell", { name: watched(0) }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: watched(1) })).toHaveCount(0);

  // Region.
  await page.getByRole("link", { name: "Clear" }).click();
  await page.getByLabel("Region").fill("Volta");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(rows).toHaveCount(1 + 7); // m = 3, 7, ..., 27
  await expect(page.getByRole("cell", { name: "Volta" })).toHaveCount(7);

  // Transaction type.
  await page.getByRole("link", { name: "Clear" }).click();
  await page.getByLabel("Transaction type").selectOption("cash_out");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(rows).toHaveCount(1 + 10);
  await expect(page.getByRole("cell", { name: "Cash out", exact: true })).toHaveCount(10);

  // Together, and the filters survive in the address.
  await page.getByLabel("Region").fill("Ashanti");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(page).toHaveURL(/region=Ashanti/);
  await expect(page).toHaveURL(/transaction_type=cash_out/);
  await expect(rows).toHaveCount(1 + 3); // m = 1, 13, 25

  // No match is said plainly.
  await page.getByLabel("Wallet").fill("w-nobody");
  await page.getByRole("button", { name: "Filter" }).click();
  await expect(page.getByText("No Medium Transactions match.")).toBeVisible();
});
