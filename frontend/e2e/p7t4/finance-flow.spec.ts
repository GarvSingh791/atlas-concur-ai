import { test } from "@playwright/test";
import { bootApp, navTo, expect } from "./helpers";

/**
 * P7-T4 finance flow — the finance_admin journey through the real UI.
 *
 * Runs under p7t4-finance (storageState=.auth/finance.json). Finance sees the
 * org-wide analytics dashboard and the policy admin surface (NOT the
 * "available to finance administrators" empty-state shown to other roles), and
 * its nav exposes the full admin set. Read-only flow — no fixtures needed.
 */

test.describe("finance: org-wide analytics + policy/admin reach", () => {
  test("/analytics renders the real Finance Analytics dashboard", async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await bootApp(page);
    await navTo(page, "/analytics");
    await page.waitForURL(/\/analytics/, { timeout: 45_000 });

    // The real dashboard heading — NOT the non-finance empty-state copy.
    await expect(
      page.getByRole("heading", { name: /Finance Analytics/i }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByText(/available to finance administrators/i),
    ).toHaveCount(0);
  });

  test("/policy renders the Policy & Admin surface (not the finance-only gate)", async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await bootApp(page);
    await navTo(page, "/policy");
    await page.waitForURL(/\/policy/, { timeout: 45_000 });

    await expect(
      page.getByRole("heading", { name: /Policy & Admin/i }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByText(
        /Policy administration is available to finance administrators/i,
      ),
    ).toHaveCount(0);
  });

  test("/audit renders the real Audit Trail (GET /audit, finance-scoped)", async ({
    page,
  }) => {
    test.setTimeout(90_000);
    await bootApp(page);

    await navTo(page, "/audit");
    await page.waitForURL(/\/audit/, { timeout: 45_000 });

    // The real page heading + filter bar — NOT the old placeholder copy.
    await expect(
      page.getByRole("heading", { level: 1, name: /Audit Trail/i }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      page.getByRole("form", { name: /Filter audit events/i }),
    ).toBeVisible();
    await expect(page.getByText(/Coming soon/i)).toHaveCount(0);

    // Org events render as a table; a fresh org shows the empty state instead.
    // Either way the request succeeded (no error / finance-only gate).
    await expect(
      page.getByRole("table").or(page.getByText(/No audit events yet/i)),
    ).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/Couldn.t load the audit trail/i)).toHaveCount(
      0,
    );
  });

  // Product gap 3: /users is the real user-administration table now.
  test("/users renders the user administration table", async ({ page }) => {
    test.setTimeout(90_000);
    await bootApp(page);

    await navTo(page, "/users");
    await page.waitForURL(/\/users/, { timeout: 45_000 });
    await expect(page.getByRole("table", { name: "Users" })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByRole("button", { name: /Add user/i })).toBeVisible();
  });

  test("finance nav exposes the full admin set", async ({ page }) => {
    test.setTimeout(60_000);
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

    for (const required of [
      "/approvals",
      "/analytics",
      "/policy",
      "/audit",
      "/users",
    ]) {
      expect(hrefs, `finance nav must expose ${required}`).toContain(required);
    }
  });
  // C7-26: finance typing an employee-only URL hits the route-level gate and
  // the top-bar title names the page (it used to fall back to "Dashboard").
  for (const [href, label] of [
    ["/expenses", "Expenses"],
    ["/receipts", "Receipts"],
    ["/planner", "Travel Planner"],
  ] as const) {
    test(`${href}: finance sees no-access with the correct title`, async ({
      page,
    }) => {
      test.setTimeout(60_000);
      await page.goto(href, { waitUntil: "domcontentloaded" });
      await expect(
        page.getByText(/You don.t have access to this page/i),
      ).toBeVisible({ timeout: 30_000 });
      await expect(
        page.getByRole("heading", { level: 1, name: label, exact: true }),
      ).toBeVisible();
      await expect(page).toHaveURL(new RegExp(href));
    });
  }
});
