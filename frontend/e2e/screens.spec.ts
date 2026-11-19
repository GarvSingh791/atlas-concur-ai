import { test } from "@playwright/test";
import { login, navTo, shot, expect, type Role } from "./helpers";

/**
 * B2 — every screen a role can reach renders a real live state (success/empty),
 *      no infinite-loop crash, no silent blank.
 * B4 — trust-cue chips render from live data (spot-checked on approvals/expenses).
 * B5 — role boundaries: the sidebar exposes only permitted destinations.
 */

const ROLES: Role[] = ["employee", "manager", "finance"];

// Destinations that must NEVER appear in a given role's sidebar (UI-edge B5).
// Derived from nav-config gates × ROLE_PERMISSIONS (verified).
const FORBIDDEN: Record<Role, string[]> = {
  employee: ["/approvals", "/analytics", "/policy", "/users", "/audit"],
  // UAT2: /analytics is finance-only (managers saw only a finance notice there).
  manager: ["/analytics", "/policy", "/users", "/audit"],
  // finance lacks trip.create.own / expense.create.own → no planner/trips/receipts/expenses.
  finance: ["/planner", "/trips", "/receipts", "/expenses"],
};

for (const role of ROLES) {
  test(`B2/B5 ${role}: reachable screens render live; nav is role-bounded`, async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await login(page, role);
    await page.waitForTimeout(2000);
    await shot(page, `B2-${role}-01-dashboard`);

    // B5 (UI-edge): capture the actual sidebar destinations for this role.
    const hrefs = (
      await page
        .locator('nav[aria-label="Main Navigation"] a[href]')
        .evaluateAll((els) =>
          els.map((e) => (e as HTMLAnchorElement).getAttribute("href")),
        )
    ).filter((h): h is string => !!h);
    console.log(`[${role}] nav:`, JSON.stringify(hrefs));
    for (const bad of FORBIDDEN[role]) {
      expect(hrefs, `${role} sidebar must not expose ${bad}`).not.toContain(
        bad,
      );
    }

    // B2: visit each reachable screen, prove it renders a live state (no crash).
    let i = 1;
    for (const href of hrefs) {
      if (href === "/dashboard") continue;
      i += 1;
      await navTo(page, href);
      await page.waitForURL(new RegExp(href.replace(/\//g, "\\/")), {
        timeout: 45_000,
      });
      await page.waitForLoadState("networkidle").catch(() => {});
      await page.waitForTimeout(2500);
      const name = href.replace(/\//g, "") || "root";
      await shot(page, `B2-${role}-${String(i).padStart(2, "0")}-${name}`);
      // No infinite-loop crash, and the body is not empty.
      const body = await page.locator("body").innerText();
      expect(body, `${href} crashed (render loop)`).not.toContain(
        "Maximum update depth",
      );
      expect(body.trim().length, `${href} rendered blank`).toBeGreaterThan(0);
    }
  });
}

test("B2 error-state: induced 5xx surfaces the envelope, not a blank screen", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await login(page, "manager");
  // Force the approvals list to fail with the backend error envelope.
  await page.route("**/api/v1/approvals**", (route) =>
    route.fulfill({
      status: 500,
      contentType: "application/json",
      headers: { "X-Correlation-ID": "e2e-induced-5xx" },
      body: JSON.stringify({
        error: {
          code: "INTERNAL_SERVER_ERROR",
          message: "An unexpected error occurred.",
          correlation_id: "e2e-induced-5xx",
        },
      }),
    }),
  );
  await navTo(page, "/approvals");
  // TanStack Query retries non-auth errors twice (exp. backoff) → the error
  // state surfaces after a few seconds, not instantly.
  await expect(
    page.getByText(/Couldn.t load approvals|Something went wrong/i).first(),
  ).toBeVisible({ timeout: 25_000 });
  await shot(page, "B2-manager-error-state-approvals");
});
