import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { chromium, type FullConfig } from "@playwright/test";

import { AUTH_DIR, signIn, stateFile, type Seeded, type Who } from "./support";

const REPO = resolve(__dirname, "../..");
// A fresh suffix gives a fresh set of Cases when the tests are run again against the same stack.
const SUFFIX = process.env.E2E_RUN ?? "";

/**
 * Signs the demo Analyst, Supervisor and Admin in once through Keycloak (their sessions are saved
 * for the tests that are not about signing in), then seeds the Cases the tests work on. Cases are
 * seeded the way scoring makes them, by `txrisk seed-cases` run as the database owner through
 * compose: the API has no way to create one, and the tests use no backdoor.
 */

type Made = { cases: { case_id: number; wallet_id: string }[] };

// E2E_SEED_COMMAND replaces the compose service where it can't run, e.g. `uv run txrisk seed-cases`
// with TXRISK_DATABASE_URL set to the owner's database URL.
function seedCases(tag: string, args: string[]): Made {
  const [command, ...prefix] = process.env.E2E_SEED_COMMAND?.split(" ") ?? [
    "docker",
    ...["compose", "run", "--rm", "-T", "seed-cases"],
  ];
  const out = execFileSync(command!, [...prefix, "--tag", tag, ...args], {
    cwd: REPO,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "inherit"],
  });
  const last = out.trim().split("\n").pop() ?? "{}";
  return JSON.parse(last) as Made;
}

export default async function globalSetup(config: FullConfig): Promise<void> {
  mkdirSync(AUTH_DIR, { recursive: true });
  const project = config.projects[0]!;
  const baseURL = project.use.baseURL!;
  const browser = await chromium.launch(project.use.launchOptions);

  let analystId = "";
  for (const who of ["analyst", "supervisor", "admin"] as Who[]) {
    const context = await browser.newContext({ baseURL });
    const page = await context.newPage();
    await signIn(page, who);
    if (who === "analyst") {
      // The Analyst's own subject ID, which the header shows so a Supervisor can assign Cases.
      analystId = (await page.getByTestId("user-id").locator("code").textContent()) ?? "";
    }
    await context.storageState({ path: stateFile(who) });
    await context.close();
  }
  await browser.close();
  if (!analystId) {
    throw new Error("the Analyst's ID was not shown in the header after signing in");
  }

  const cases: Seeded["cases"] = {
    // Assigned to the demo Analyst: opened, noted and closed by the Analyst's tests.
    analyst: seedCases(`e2ea${SUFFIX}`, [
      ...["--count", "4", "--assignee-id", analystId, "--assignee-name", "Demo Analyst"],
    ]).cases,
    // Unassigned: the Supervisor assigns one.
    supervisor: seedCases(`e2es${SUFFIX}`, ["--count", "2"]).cases,
    // Unassigned, for the role tests; and one assigned to another Analyst.
    unassigned: seedCases(`e2eu${SUFFIX}`, ["--count", "2"]).cases,
    other: seedCases(`e2eo${SUFFIX}`, [
      ...["--count", "1", "--assignee-id", "sub-another-analyst", "--assignee-name", "Another Analyst"],
    ]).cases,
    // Assigned to the demo Analyst: closed as fraud, drafted and exported by the report test.
    reports: seedCases(`e2er${SUFFIX}`, [
      ...["--count", "1", "--assignee-id", analystId, "--assignee-name", "Demo Analyst"],
    ]).cases,
    // Thirty Medium Transactions across regions and types: more than a page.
    watch: seedCases(`e2ew${SUFFIX}`, ["--count", "0", "--medium", "30"]).cases,
  };
  const seeded: Seeded = { analystId, suffix: SUFFIX, cases };
  writeFileSync(resolve(AUTH_DIR, "seeded.json"), JSON.stringify(seeded));
}
