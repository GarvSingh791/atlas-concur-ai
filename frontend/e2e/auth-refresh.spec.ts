import { test } from "@playwright/test";
import { login, navTo, shot, expect } from "./helpers";

/**
 * B1 — the headline auth-refresh seam test (P6-T1).
 *
 * Strategy: log in for real (valid tokens), then navigate CLIENT-SIDE (the
 * in-memory access token survives, so a refresh is attributable to the forced
 * 401 — not to mount-time hydration). We force the first GET /approvals to 401,
 * exactly as an expired access token would, and assert the client silently
 * refreshes + retries (success case) or redirects to /login (refresh-dead case).
 */

const ENVELOPE_401 = (cid: string) => ({
  status: 401,
  contentType: "application/json",
  headers: { "X-Correlation-ID": cid },
  body: JSON.stringify({
    error: {
      code: "UNAUTHORIZED",
      message: "Access token expired",
      correlation_id: cid,
    },
  }),
});

test("B1a: 401 → silent refresh → retry → screen renders (no user-visible failure)", async ({
  page,
}) => {
  await login(page, "manager");

  // Count refreshes triggered AFTER login (attributable to the forced 401).
  let refreshCount = 0;
  page.on("request", (req) => {
    if (req.url().includes("/auth/refresh") && req.method() === "POST")
      refreshCount++;
  });

  // Force the FIRST GET /approvals to look like an expired-token 401; the
  // retried request (after refresh) passes through to the live backend.
  let forced = false;
  await page.route("**/api/v1/approvals**", async (route) => {
    if (!forced && route.request().method() === "GET") {
      forced = true;
      await route.fulfill(ENVELOPE_401("e2e-forced-expiry"));
    } else {
      await route.continue();
    }
  });

  // Client-side navigate to the approvals screen (preserves access token).
  const retried = page.waitForResponse(
    (r) =>
      r.url().includes("/api/v1/approvals") &&
      r.request().method() === "GET" &&
      r.status() === 200,
    { timeout: 45_000 },
  );
  await navTo(page, "/approvals");
  await retried;

  // Proof: a refresh fired, the retry succeeded, and we stayed on approvals.
  expect(forced, "the forced 401 was served").toBe(true);
  expect(refreshCount, "a silent POST /auth/refresh fired").toBeGreaterThan(0);
  await expect(page).toHaveURL(/\/approvals/);
  await expect(page).not.toHaveURL(/\/login/);
  await shot(page, "B1a-auth-refresh-success");
});

test("B1b: refresh fails → session cleared → redirect to /login", async ({
  page,
}) => {
  await login(page, "manager");

  // Refresh endpoint is dead.
  await page.route("**/api/v1/auth/refresh", (route) =>
    route.fulfill(ENVELOPE_401("e2e-refresh-dead")),
  );
  // Force a 401 on the next data call to trigger the (doomed) refresh.
  let forced = false;
  await page.route("**/api/v1/approvals**", async (route) => {
    if (!forced && route.request().method() === "GET") {
      forced = true;
      await route.fulfill(ENVELOPE_401("e2e-forced-expiry"));
    } else {
      await route.continue();
    }
  });

  await navTo(page, "/approvals");
  await page.waitForURL(/\/login/, { timeout: 30_000 });
  await expect(page).toHaveURL(/\/login/);
  await shot(page, "B1b-refresh-failure-redirect");
});
