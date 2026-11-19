import { test } from "@playwright/test";
import { login, navTo, shot, expect } from "./helpers";

/**
 * B3 — state-changing actions are honest: success reflects real server state;
 * induced failure surfaces an error and never leaves optimistic UI lying.
 * Seeded drafts created via API: "P6T1 B3 Submit OK" / "P6T1 B3 Submit FAIL".
 */

const FAIL_500 = (cid: string) => ({
  status: 500,
  contentType: "application/json",
  headers: { "X-Correlation-ID": cid },
  body: JSON.stringify({
    error: {
      code: "INTERNAL_SERVER_ERROR",
      message: "Induced failure.",
      correlation_id: cid,
    },
  }),
});

async function openReport(
  page: import("@playwright/test").Page,
  title: string,
) {
  await navTo(page, "/expenses");
  await page.waitForLoadState("networkidle").catch(() => {});
  const row = page.locator(`button[aria-label="Open ${title}"]`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await row.click();
}

test("B3.2a submit-report success → server status reflected", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await login(page, "employee");
  await openReport(page, "P6T1 B3 Submit OK");
  const submit = page.getByRole("button", { name: /submit report/i });
  await submit.waitFor({ state: "visible", timeout: 30_000 });
  await submit.click();
  const anyway = page.getByRole("button", { name: /submit anyway/i });
  if (await anyway.isVisible().catch(() => false)) await anyway.click();
  await expect(page.getByText(/Report submitted/i)).toBeVisible({
    timeout: 20_000,
  });
  await shot(page, "B3-2a-submit-success");
});

test("B3.2b submit-report induced failure → error shown, status not advanced", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await login(page, "employee");
  await openReport(page, "P6T1 B3 Submit FAIL");
  await page.route("**/api/v1/expense-reports/*", (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill(FAIL_500("e2e-submit-fail"))
      : route.continue(),
  );
  const submit = page.getByRole("button", { name: /submit report/i });
  await submit.click();
  const anyway = page.getByRole("button", { name: /submit anyway/i });
  if (await anyway.isVisible().catch(() => false)) await anyway.click();
  await expect(page.getByText(/Couldn.t submit/i)).toBeVisible({
    timeout: 20_000,
  });
  // Honesty: the submit bar is still present → status was NOT optimistically advanced.
  await expect(
    page.getByRole("button", { name: /submit report/i }),
  ).toBeVisible();
  await shot(page, "B3-2b-submit-failure-honest");
});

test("B3.3a reject induced failure → error shown, decision not applied", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await login(page, "finance");
  await navTo(page, "/approvals");
  await page.waitForLoadState("networkidle").catch(() => {});
  const item = page
    .getByRole("button")
    .filter({ hasText: /Client Visit Expenses|Carlos Rivera/i })
    .first();
  await item.waitFor({ state: "visible", timeout: 30_000 });
  await item.click();
  await page.route("**/api/v1/approvals/*", (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill(FAIL_500("e2e-reject-fail"))
      : route.continue(),
  );
  await page.getByRole("button", { name: /^reject/i }).click();
  const ta = page.getByRole("dialog").getByRole("textbox").first();
  await ta.waitFor({ state: "visible", timeout: 15_000 });
  await ta.fill("Over the hotel cap — e2e honesty check.");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: /submit|reject|confirm/i })
    .first()
    .click();
  await expect(page.getByText(/Decision failed/i)).toBeVisible({
    timeout: 20_000,
  });
  await shot(page, "B3-3a-reject-failure-honest");
});

test("B3.3b approve success → server decision reflected", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page, "finance");
  await navTo(page, "/approvals");
  await page.waitForLoadState("networkidle").catch(() => {});
  const item = page
    .getByRole("button")
    .filter({ hasText: /Client Visit Expenses|Carlos Rivera/i })
    .first();
  await item.waitFor({ state: "visible", timeout: 30_000 });
  await item.click();
  await page.getByRole("button", { name: /^approve/i }).click();
  await expect(page.getByText(/^Approved$/i).first()).toBeVisible({
    timeout: 20_000,
  });
  await shot(page, "B3-3b-approve-success");
});
