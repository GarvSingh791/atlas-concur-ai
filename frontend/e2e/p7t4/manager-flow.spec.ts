import { test } from "@playwright/test";
import {
  apiLoginStandalone,
  approveInUi,
  getApproval,
  bootApp,
  navTo,
  seedDraftReport,
  seedTrip,
  submitTrip,
  submitReport,
  findApproval,
  expect,
} from "./helpers";

/**
 * P7-T4 manager flow — the approver journey through the real UI:
 * approve a pending TRIP and a pending REPORT.
 *
 * Runs under p7t4-manager (storageState=.auth/manager.json). Both subjects are
 * self-seeded via the API and escalated to a real pending item (total/cost >
 * the auto-approve threshold, so the approval gate does NOT auto-approve). The
 * owner's submit opens the approval (C7R) and routes it to the employee's
 * manager, so the item lands in this manager's queue — re-runnable and
 * deterministic. The approval is then driven through the UI, not the API.
 */

test.describe("manager: approve pending items", () => {
  test("approve a pending REPORT (escalated, > $500) via the UI", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    const emp = await apiLoginStandalone("employee");
    const mgr = await apiLoginStandalone("manager");
    const title = `E2E Mgr Report ${Date.now()}`;
    try {
      const report = await seedDraftReport(emp.tokens.access_token, title);
      await submitReport(emp.tokens.access_token, report.id);
      const approval = await findApproval(
        mgr.tokens.access_token,
        "expense_report",
        report.id,
      );
      // Sanity: the > $500 report must escalate, not auto-approve.
      expect(approval.is_auto_approved, "report should escalate").toBe(false);
      expect(approval.current_status).toBe("pending");

      await bootApp(page);
      await navTo(page, "/approvals");
      await page.waitForLoadState("networkidle").catch(() => {});

      // T2: target THIS approval's row by id, wait for its PATCH response and
      // assert the decision (a page-wide /^Approved$/ matched old badges).
      await approveInUi(page, approval.id);
      const decided = await getApproval(mgr.tokens.access_token, approval.id);
      expect(decided.current_status).toBe("approved");
    } finally {
      await emp.ctx.dispose();
      await mgr.ctx.dispose();
    }
  });

  test("approve a pending TRIP (escalated) via the UI", async ({ page }) => {
    test.setTimeout(120_000);

    const emp = await apiLoginStandalone("employee");
    const mgr = await apiLoginStandalone("manager");
    const purpose = `E2E Mgr Trip ${Date.now()}`;
    try {
      const trip = await seedTrip(emp.tokens.access_token, purpose);
      // Submitting the draft trip (draft → pending_approval) as its owner opens
      // its approval in the same transaction (C7R).
      await submitTrip(emp.tokens.access_token, trip.id);
      const approval = await findApproval(
        mgr.tokens.access_token,
        "trip",
        trip.id,
      );
      expect(approval.is_auto_approved, "trip should escalate").toBe(false);
      expect(approval.current_status).toBe("pending");

      await bootApp(page);
      await navTo(page, "/approvals");
      await page.waitForLoadState("networkidle").catch(() => {});

      // T3: every seeded trip shares the "SFO → JFK" title, so `.first()` on
      // it could pick an already-decided row from an earlier run. Target the
      // approval created by THIS run (by id) instead.
      await approveInUi(page, approval.id);
      const decided = await getApproval(mgr.tokens.access_token, approval.id);
      expect(decided.current_status).toBe("approved");
    } finally {
      await emp.ctx.dispose();
      await mgr.ctx.dispose();
    }
  });
});
