import { expect, test } from "@playwright/test";

import { expectAccessible, signIn } from "./support";

test("an Analyst signs in through Keycloak, and the browser never holds a token", async ({
  page,
  context,
}) => {
  const csp: string[] = [];
  page.on("console", (m) => {
    if (m.text().includes("Content Security Policy")) csp.push(m.text());
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Case workbench" })).toBeVisible();
  await expectAccessible(page);

  await signIn(page, "analyst");
  await expect(page).toHaveURL(/\/cases$/);
  await expect(page.getByTestId("signed-in-as")).toContainText("Demo Analyst");
  await expect(page.getByTestId("signed-in-as")).toContainText("analyst");

  // Only an httpOnly, SameSite cookie holds the session, and nothing in page storage does.
  const cookies = (await context.cookies()).filter((c) => c.domain === "localhost" && c.name.startsWith("txw_"));
  expect(cookies.map((c) => c.name)).toEqual(["txw_session"]);
  expect(cookies[0]).toMatchObject({ httpOnly: true, sameSite: "Lax", path: "/" });
  const visibleToScripts = await page.evaluate(() => ({
    cookie: document.cookie,
    local: JSON.stringify(Object.entries(localStorage)),
    session: JSON.stringify(Object.entries(sessionStorage)),
  }));
  expect(visibleToScripts.cookie).toBe("");
  expect(visibleToScripts.local).toBe("[]");
  expect(visibleToScripts.session).toBe("[]");
  const html = await page.content();
  expect(html).not.toMatch(/eyJ[\w-]{10,}\.[\w-]{10,}\./);
  expect(csp).toEqual([]);
});

test("pages carry a strict Content-Security-Policy and the other security headers", async ({ request }) => {
  const response = await request.get("/", { maxRedirects: 0 });
  const h = response.headers();
  expect(h["content-security-policy"]).toMatch(/script-src 'self' 'nonce-[\w+/=]+' 'strict-dynamic'/);
  expect(h["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(h["x-content-type-options"]).toBe("nosniff");
  expect(h["x-frame-options"]).toBe("DENY");
  expect(h["referrer-policy"]).toBe("same-origin");
  expect(h["cache-control"]).toBe("no-store");
});

test("signing out ends the Keycloak session too", async ({ page }) => {
  await signIn(page, "analyst");
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/signed-out$/);
  await expect(page.getByText("You signed out")).toBeVisible();

  // The workbench has no session, and Keycloak asks for a sign-in again (its session ended).
  await page.goto("/cases");
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
  await page.getByRole("link", { name: "Sign in" }).click();
  await expect(page.locator("#username")).toBeVisible();
});

test("a page that needs a sign-in sends a signed-out person to the landing page", async ({ page }) => {
  await page.goto("/cases/1");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
});

test("an Admin signs in and has no Case screens", async ({ page }) => {
  await signIn(page, "admin");
  await expect(page).toHaveURL(/\/no-access$/);
  await expect(page.getByRole("heading", { name: "No Case screens for your role" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Cases" })).toHaveCount(0);
  await page.goto("/cases");
  await expect(page).toHaveURL(/\/no-access$/);
  await page.goto("/watchlist");
  await expect(page).toHaveURL(/\/no-access$/);
});
