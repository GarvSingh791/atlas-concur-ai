import { test as setup, expect } from "@playwright/test";
import { promises as fs } from "fs";
import path from "path";
import { apiLogin, RT_KEY, type Role } from "./helpers";

/**
 * P7-T4 auth setup — log in each role ONCE, save its storageState.
 *
 * Strategy (faithful to the app's real boot path):
 *   1. API login (request context) → {access_token, refresh_token}.
 *   2. Open /login, then seed localStorage["atlas_rt"] with the refresh token.
 *      On mount, SessionProvider.hydrateSession reads atlas_rt → POST /auth/refresh
 *      → GET /me → authenticated. The access token is memory-only by design, so
 *      the refresh token alone is the durable session — exactly what the real app
 *      persists. (Refresh TTL = 7d, comfortably longer than a suite run.)
 *   3. Save storageState to .auth/{role}.json; the per-role projects reuse it,
 *      so each role logs in exactly once for the whole run.
 *
 * .auth/ holds live refresh tokens — it is gitignored, never committed.
 */

const AUTH_DIR = path.join(process.cwd(), ".auth");
const ROLES: Role[] = ["employee", "manager", "finance"];

for (const role of ROLES) {
  setup(`authenticate ${role}`, async ({ page, request }) => {
    await fs.mkdir(AUTH_DIR, { recursive: true });

    // 1. API login.
    const tokens = await apiLogin(request, role);

    // 2. Seed the refresh token into localStorage on the app origin, then prove
    //    the app actually hydrates an authenticated session from it (the app
    //    shell / sidebar only mounts when authenticated).
    await page.goto("/login", { waitUntil: "domcontentloaded" });
    await page.evaluate(
      ([key, value]) => window.localStorage.setItem(key, value),
      [RT_KEY, tokens.refresh_token] as const,
    );
    await page.goto("/dashboard", { waitUntil: "domcontentloaded" });
    await expect(
      page.locator('nav[aria-label="Main Navigation"]'),
      `${role} session did not hydrate from the seeded refresh token`,
    ).toBeVisible({ timeout: 60_000 });
    await expect(page).toHaveURL(/\/dashboard/);

    // 3. Persist storageState for reuse by the role's project.
    await page.context().storageState({
      path: path.join(AUTH_DIR, `${role}.json`),
    });
  });
}
