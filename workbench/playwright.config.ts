import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the real stack: the workbench, the API, Keycloak and Postgres, started
 * by `docker compose up --build --wait api keycloak workbench`. The demo users' sign-in values are
 * read from the environment (CI and `keycloak.env` generate them); none is in this repository.
 * PW_CHROMIUM_PATH points at an installed Chromium when Playwright's own download is not allowed.
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  // The tests change shared Cases, so they run one after another.
  workers: 1,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: process.env.WORKBENCH_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    launchOptions: process.env.PW_CHROMIUM_PATH
      ? { executablePath: process.env.PW_CHROMIUM_PATH, args: ["--no-sandbox"] }
      : {},
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
