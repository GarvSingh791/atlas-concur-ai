import { readFileSync } from "node:fs";
import path from "node:path";
import { test, type Locator, type Page } from "@playwright/test";
import {
  addChip,
  apiLoginStandalone,
  bootApp,
  navTo,
  seedDraftReport,
  expect,
} from "./helpers";

/**
 * P7-T4 employee flow — the core IC journey through the real UI:
 *   plan a trip → capture a receipt → assemble + submit a report.
 *
 * Runs under the p7t4-employee project (storageState=.auth/employee.json), so
 * the employee session is reused (logged in once by the setup project). The
 * mutating submit step self-seeds its own draft via the API so the spec is
 * idempotent across re-runs. Everything else asserts real, live state — no
 * assertion passes on an empty page.
 */

type Box = { x: number; y: number; width: number; height: number };

function intersects(a: Box, b: Box): boolean {
  return (
    a.x < b.x + b.width &&
    b.x < a.x + a.width &&
    a.y < b.y + b.height &&
    b.y < a.y + a.height
  );
}

/**
 * UAT-2 Issue 3 — the "Confidence: NN%" label was painted over the Edit/Save
 * buttons and the value column collapsed to 0px at 1024–~1700px. For each
 * width: a TRIAL click on the amount's edit button (Playwright's hit-target
 * check fails if another element would receive the click; a trial never fires
 * the handler), no Confidence label overlaps it, and #main-content has no
 * horizontal scroll. At 1024 and 1202 the editor is opened for real and its
 * Save / Cancel buttons get the same checks; it is closed with Cancel — Save is
 * never clicked, so nothing is PATCHed.
 */
async function assertReceiptFieldsClickable(page: Page) {
  // "Edit Amount" normally; "Enter Amount" if the provider flagged the total
  // for review. Exactly one must exist so this check can never pass vacuously.
  const editAmount = page.getByRole("button", {
    name: /^(Edit|Enter) Amount$/,
  });
  await expect(editAmount).toHaveCount(1);
  const confidenceLabels = page.getByText(/^Confidence: \d+%$/);

  async function assertClearOfConfidence(target: Locator, what: string) {
    await target.click({ trial: true, timeout: 5_000 });
    const box = await target.boundingBox();
    expect(box, `${what} has a layout box`).not.toBeNull();
    for (const label of await confidenceLabels.all()) {
      const lb = await label.boundingBox();
      if (lb) {
        expect(
          intersects(box as Box, lb),
          `${what} must not sit under a Confidence label`,
        ).toBe(false);
      }
    }
  }

  for (const width of [768, 1024, 1202, 1280, 1440, 1920]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.getByText(/\$42\.50/).first()).toBeVisible();
    await assertClearOfConfidence(editAmount, `Edit Amount @${width}px`);
    const overflow = await page
      .locator("#main-content")
      .evaluate((el) => el.scrollWidth - el.clientWidth);
    expect(
      overflow,
      `#main-content must not scroll horizontally @${width}px`,
    ).toBeLessThanOrEqual(1);

    if (width === 1024 || width === 1202) {
      await editAmount.click();
      const save = page.getByRole("button", { name: "Save Amount" });
      const cancel = page.getByRole("button", {
        name: "Cancel editing Amount",
      });
      await expect(save).toBeVisible();
      await assertClearOfConfidence(save, `Save Amount @${width}px`);
      await assertClearOfConfidence(cancel, `Cancel Amount @${width}px`);
      await cancel.click();
      await expect(save).toHaveCount(0);
    }
  }
  await page.setViewportSize({ width: 1440, height: 900 });
}

test.describe("employee: plan → receipt → report", () => {
  test("plan a trip: chips complete the intent → option is selectable → trip saved", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await bootApp(page);
    await navTo(page, "/planner");

    // Chip-driven structured intent. Currency pre-defaults to USD; filling the
    // four required slots completes the intent and re-streams the plan. The
    // seeded SFO→JFK 2026-06-10 route returns real option cards.
    await addChip(page, "From", "SFO");
    await addChip(page, "To", "JFK");
    await addChip(page, "Depart", "2026-06-10");
    await addChip(page, "Travelers", "1");

    // The shortlist arrives from the (deterministic, local) planner — a real
    // selectable option card, not an empty panel.
    const select = page.getByRole("button", { name: /^Select / }).first();
    await expect(select).toBeVisible({ timeout: 45_000 });
    await select.click();

    // Selecting persists a draft Trip → success toast, and the card flips to
    // the "Selected" state. Both prove the selection round-tripped to the server.
    // (The toast text renders twice — visible title + aria-live region — so take
    // the first match to avoid a strict-mode violation.)
    await expect(page.getByText("Trip saved").first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(
      page.getByRole("button", { name: "Selected", exact: true }).first(),
    ).toBeVisible({ timeout: 20_000 });
  });

  /**
   * Receipt capture (T1) — a REAL receipt image that is valid under BOTH OCR
   * back-ends, asserting only outcomes that are correct under both.
   *
   * Fixture: e2e/fixtures/receipt-meal-42_50.png — a genuine 640x505 PNG of a
   * restaurant receipt (TOTAL USD 42.50). Its last pixel row carries the bytes
   * "ocr-profile=meal" (about five bottom-right pixels) in a STORED deflate
   * block, so the text is verbatim in the file. Phase 11 sanitises uploads
   * (tEXt and other metadata chunks are dropped, pixel data is kept), so the
   * hint has to live in the pixels. The CI stub (OCR_PROVIDER=local) keys off
   * the decoded bytes, so it returns its canned "meal" extraction (amount
   * 42.50), while a real OCR provider (staging) reads the printed receipt
   * (total 42.50). backend/tests/test_ps_upload_api_db.py uploads this exact
   * file through POST /receipts with the local stub and asserts 42.50. The old
   * test sent 8 PNG-header bytes + ASCII, which real OCR correctly rejects
   * (ocr_status=failed), and asserted the stub-only "Deterministic OCR" chip.
   *
   * Accepted outcomes after the upload (201) — exactly one must render:
   *   (a) extraction finished: "Extracted fields" review with the 42.50 amount;
   *   (b) the explicit, honest failure state: "Extraction failed" with Retry +
   *       Remove (a real-OCR outage/unreadable read — never a silent blank).
   * The processing-path chip (Deterministic OCR vs AI Gap-Fill) depends on the
   * provider's confidences, so it is deliberately NOT asserted.
   *
   * Storage: the E2E stack runs STORAGE_PROVIDER=local (stdlib-only), so the
   * uploaded bytes round-trip in-process to the OCR core.
   */
  test("capture a receipt: real image upload → extracted fields (or explicit retryable failure)", async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await bootApp(page);
    await navTo(page, "/receipts");

    // Unique display name per run so the queue row is unambiguously ours.
    const fileName = `e2e-receipt-${Date.now()}.png`;
    const fixture = readFileSync(
      path.join(__dirname, "..", "fixtures", "receipt-meal-42_50.png"),
    );

    // Prove the UI genuinely fires the real upload endpoint (not a no-op) and
    // that the backend accepts it (2xx) — so a green test can never mask a
    // silent failure or a fake client-only "extracted" state.
    const uploadResponse = page.waitForResponse(
      (res) =>
        res.url().includes("/api/v1/receipts") &&
        res.request().method() === "POST",
      { timeout: 60_000 },
    );

    await page.setInputFiles('input[type="file"]:not([capture])', {
      name: fileName,
      mimeType: "image/png",
      buffer: fixture,
    });

    const res = await uploadResponse;
    expect(
      res.status(),
      "POST /api/v1/receipts must succeed (2xx) — OCR failure is reported in the body, not as an HTTP error",
    ).toBe(201);
    const body = (await res.json()) as { ocr_status?: string };

    // The queue took THIS file in.
    await expect(page.getByText(fileName).first()).toBeVisible({
      timeout: 30_000,
    });

    const extracted = page.getByRole("heading", { name: "Extracted fields" });
    // A FAILED receipt is not auto-selected (use-upload-queue only selects a
    // processed one), so the review panel's "Extraction failed" heading never
    // renders for it. The failure is signalled on THIS file's queue row: its
    // Retry button only exists while the row's status is "failed".
    const retry = page.getByRole("button", { name: `Retry ${fileName}` });
    await expect(extracted.or(retry)).toBeVisible({ timeout: 60_000 });

    if (await extracted.isVisible()) {
      // (a) Extraction finished with real fields: the 42.50 total surfaced
      // (same value from the stub's canned meal set and from the printed
      // receipt). UAT-2 Issue 3: the value column used to collapse to 0px
      // (hence the old toBeAttached work-around); it must now be VISIBLE.
      expect(body.ocr_status, "server agrees extraction succeeded").not.toBe(
        "failed",
      );
      await expect(page.getByText(/\$42\.50/).first()).toBeVisible();
      await expect(retry).toHaveCount(0);
      await assertReceiptFieldsClickable(page);
    } else {
      // (b) Honest, retryable failure: the row says so and offers Retry +
      // Remove for THIS file.
      expect(body.ocr_status, "UI failure must mirror the server").toBe(
        "failed",
      );
      await expect(
        page.getByText("Extraction failed. Please retry.").first(),
      ).toBeVisible();
      await expect(retry).toBeVisible();
      await expect(
        page.getByRole("button", { name: `Remove ${fileName}` }),
      ).toBeVisible();
    }

    // Never the transport-level failure.
    await expect(page.getByText(/Upload failed/i)).toHaveCount(0);
  });

  test("assemble + submit a report: self-seeded draft → Report submitted", async ({
    page,
  }) => {
    test.setTimeout(120_000);

    // Self-seed a draft via the API so the submit is re-runnable (a submitted
    // report can't be re-submitted). > $500 is irrelevant to submission but
    // keeps the fixture shape consistent with the approval flows.
    const { ctx, tokens } = await apiLoginStandalone("employee");
    const title = `E2E Employee Submit ${Date.now()}`;
    await seedDraftReport(tokens.access_token, title);
    await ctx.dispose();

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

    // An amber readiness warning may interpose a confirm dialog — proceed.
    const anyway = page.getByRole("button", { name: /submit anyway/i });
    if (await anyway.isVisible().catch(() => false)) await anyway.click();

    await expect(page.getByText(/Report submitted/i).first()).toBeVisible({
      timeout: 20_000,
    });
  });
});
