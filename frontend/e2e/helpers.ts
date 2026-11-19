import { type Page, expect } from "@playwright/test";

// The seeded demo password. CI mints a masked, per-run throwaway value and
// exports it as SEED_DEFAULT_PASSWORD (the seed hashes the same env var), so
// the suite never logs in with the published demo password there. Locally it
// falls back to the documented dev default from .env.example.
export const PASSWORD =
  process.env.SEED_DEFAULT_PASSWORD || "change-me-seed-password";
export const SHOTS = "e2e/screenshots";

export type Role = "employee" | "manager" | "finance";
export const EMAIL: Record<Role, string> = {
  employee: "employee@atlas-demo.internal",
  manager: "manager@atlas-demo.internal",
  finance: "finance@atlas-demo.internal",
};

/** Real login through the UI form → lands authenticated on /dashboard with the
 *  app shell (sidebar) mounted. Waits generously for dev-mode on-demand compile. */
export async function login(page: Page, role: Role): Promise<void> {
  await page.goto("/login", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#login-email", { timeout: 60_000 });
  // Dev-mode: allow React to hydrate before interacting, else the click fires
  // before the submit handler attaches and nothing happens.
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.waitForTimeout(2500);
  await page.fill("#login-email", EMAIL[role]);
  await page.fill("#login-password", PASSWORD);
  await page.click("#login-submit");
  await page.waitForURL(/\/dashboard/, { timeout: 60_000 });
  // App shell / sidebar must be mounted before tests click nav links.
  await page.waitForSelector('nav[aria-label="Main Navigation"]', {
    timeout: 60_000,
  });
}

/** Robust client-side nav click to a sidebar destination by href. */
export async function navTo(page: Page, href: string): Promise<void> {
  const link = page.locator(
    `nav[aria-label="Main Navigation"] a[href="${href}"]`,
  );
  await link.waitFor({ state: "visible", timeout: 60_000 });
  await link.click();
}

export async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
}

export { expect };
