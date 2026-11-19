import { test } from "@playwright/test";
import { bootApp, expect } from "./helpers";

/**
 * P7-T4 RBAC-in-UI — the role boundary bites in the browser.
 *
 * Runs under p7t4-employee (storageState=.auth/employee.json). The employee:
 *   - never SEES forbidden destinations in the nav (server-derived nav-config);
 *   - typing a forbidden URL (/approvals, /analytics, /policy, /audit, /users)
 *     hits the route-level gate (C7-26): a "You don't have access to this page"
 *     state, and the employee STAYS on that URL (RBAC-deny, NOT an auth
 *     redirect — the distinction is the point);
 *   - an UNAUTHENTICATED context is redirected to /login (the one true redirect).
 *
 * No assertion passes on an empty page — each checks specific denied messaging.
 */

const FORBIDDEN_NAV = [
  "/approvals",
  "/analytics",
  "/policy",
  "/users",
  "/audit",
];

test.describe("RBAC-in-UI (employee)", () => {
  test("nav excludes every forbidden destination", async ({ page }) => {
    await bootApp(page);
    await expect(
      page.locator('nav[aria-label="Main Navigation"]'),
    ).toBeVisible();

    const hrefs = (
      await page
        .locator('nav[aria-label="Main Navigation"] a[href]')
        .evaluateAll((els) =>
          els.map((e) => (e as HTMLAnchorElement).getAttribute("href")),
        )
    ).filter((h): h is string => !!h);

    // Positive control: the employee DOES see its own destinations, so an empty
    // nav can't trivially satisfy the exclusions below.
    expect(hrefs, "employee should see /planner").toContain("/planner");
    expect(hrefs, "employee should see /expenses").toContain("/expenses");

    for (const bad of FORBIDDEN_NAV) {
      expect(hrefs, `employee nav must not expose ${bad}`).not.toContain(bad);
    }
  });

  // C7-26: a role typing a hidden route now hits the route-level gate (driven
  // by the same nav-config mapping that hides it from the nav): a clear
  // "You don't have access to this page" state, NOT the page itself, and NOT an
  // auth redirect — the user stays on the forbidden URL. Server enforcement is
  // unchanged (the page's own 403 / finance-only states remain as defence in
  // depth behind the gate).
  for (const href of FORBIDDEN_NAV) {
    test(`${href}: route gate shows no-access, employee stays (not redirected)`, async ({
      page,
    }) => {
      test.setTimeout(90_000);
      await page.goto(href, { waitUntil: "domcontentloaded" });
      await expect(
        page.getByText(/You don.t have access to this page/i),
      ).toBeVisible({ timeout: 30_000 });
      await expect(page).toHaveURL(new RegExp(href));
      await expect(page).not.toHaveURL(/\/login/);
      // The forbidden page's own data surfaces are not mounted. Scoped to the page
      // content: the app shell's global search box (top bar) is itself a combobox.
      await expect(page.getByRole("main").getByRole("combobox")).toHaveCount(0);
      await expect(page.getByLabel("Search approvals")).toHaveCount(0);
    });
  }

  test("unauthenticated → /dashboard redirects to /login", async ({ page }) => {
    test.setTimeout(60_000);
    // Drop the session: load the app origin, clear the refresh token, then load a
    // protected route fresh. On mount SessionProvider finds no atlas_rt →
    // clearSession() → status 'unauthenticated' → AuthGuard router.replace(
    // "/login"). (Clearing on the test's own page is more robust than a second
    // browser context — no cross-context storageState ambiguity.)
    await page.goto("/login", { waitUntil: "domcontentloaded" });
    await page.evaluate(() => window.localStorage.clear());
    await page.goto("/dashboard", { waitUntil: "domcontentloaded" });

    // Confirm the redirect happened AND the login form rendered (so the assertion
    // can't pass on a half-loaded page).
    await page.waitForURL(/\/login/, { timeout: 30_000 });
    await expect(page).toHaveURL(/\/login/);
    await expect(page.locator("#login-email")).toBeVisible({
      timeout: 15_000,
    });
  });
});
