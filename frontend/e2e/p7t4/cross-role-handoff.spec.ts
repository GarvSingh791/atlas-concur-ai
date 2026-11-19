import { test } from "@playwright/test";
import {
  apiLoginStandalone,
  approveInUi,
  getApproval,
  bootApp,
  navTo,
  seedDraftReport,
  findApproval,
  expect,
} from "./helpers";

/**
 * P7-T4 cross-role handoff — ONE subject travels employee → manager through two
 * real browser contexts.
 *
 *   Employee context (this project's storageState): opens a self-seeded draft
 *   and SUBMITS it through the UI → "Report submitted".
 *
 *   No API bridge: the UI "Submit report" itself opens the approval (C7R — the
 *   submit runs the Approval Agent in the same transaction and routes the
 *   > $500 report to the employee's manager). The manager token only LOOKS UP
 *   that approval's id (GET /approvals) so the UI step can target its row.
 *
 *   Manager context (browser.newContext with .auth/manager.json): finds that
 *   same approval (by id) in the queue and APPROVES it through the UI; the
 *   PATCH /approvals/{id} response and a follow-up GET prove it was decided.
 *
 * Real submit + real approve on a single subject = a faithful handoff.
 */

test("cross-role: employee submits (UI) → manager approves (UI) the same report", async ({
  page,
  browser,
}) => {
  test.setTimeout(150_000);

  const emp = await apiLoginStandalone("employee");
  const mgr = await apiLoginStandalone("manager");
  const title = `E2E Handoff ${Date.now()}`;

  try {
    // --- Employee: self-seed a draft, then SUBMIT it through the real UI. ---
    const report = await seedDraftReport(emp.tokens.access_token, title);

    await bootApp(page);
    await navTo(page, "/expenses");
    await page.waitForLoadState("networkidle").catch(() => {});
    const open = page.getByRole("button", {
      name: `Open ${title}`,
      exact: true,
    });
    await open.waitFor({ state: "visible", timeout: 30_000 });
    await open.click();

    const submit = page.getByRole("button", { name: /submit report/i });
    await submit.waitFor({ state: "visible", timeout: 30_000 });
    await submit.click();
    const anyway = page.getByRole("button", { name: /submit anyway/i });
    if (await anyway.isVisible().catch(() => false)) await anyway.click();
    await expect(page.getByText(/Report submitted/i).first()).toBeVisible({
      timeout: 20_000,
    });

    // --- The submit opened the approval for THIS report; find its id. ---
    const approval = await findApproval(
      mgr.tokens.access_token,
      "expense_report",
      report.id,
    );
    expect(approval.is_auto_approved, "> $500 report must escalate").toBe(
      false,
    );
    expect(approval.current_status).toBe("pending");

    // --- Manager: a second real browser context APPROVES it through the UI. ---
    const mgrContext = await browser.newContext({
      storageState: ".auth/manager.json",
    });
    const mgrPage = await mgrContext.newPage();
    try {
      await bootApp(mgrPage);
      await navTo(mgrPage, "/approvals");
      await mgrPage.waitForLoadState("networkidle").catch(() => {});

      // T2: approve THIS approval (row targeted by id), wait for its PATCH
      // response before the context can close, and assert the decision —
      // page-wide /^Approved$/ matched pre-existing badges and false-passed.
      await approveInUi(mgrPage, approval.id);
    } finally {
      await mgrContext.close();
    }

    // Server-side truth after the UI flow (independent of the browser).
    const decided = await getApproval(mgr.tokens.access_token, approval.id);
    expect(decided.current_status).toBe("approved");
  } finally {
    await emp.ctx.dispose();
    await mgr.ctx.dispose();
  }
});
