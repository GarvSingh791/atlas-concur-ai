// Captures the README screenshots and the frames of the demo GIF.
//
//   node capture.mjs <out-dir> <receipt.png>
//
// Two modes:
//   - LOCAL (default): a local Atlas stack seeded with synthetic data and the
//     no-key local providers. OCR returns canned fields, so pass the receipt that
//     make_receipt.py draws; the policy assistant has no model and abstains.
//   - LIVE (ATLAS_LIVE=1): the public staging demo (Gemini via OpenRouter, real
//     vision OCR, live retrieval). Pass a receipt from ../sample-receipts/. Model
//     calls take seconds, so every wait is on a UI state with a long timeout. Wake
//     the free-tier backend first (GET /health until 200); a cold start can take
//     about two minutes.
//
// Env: ATLAS_LIVE (1 = live mode), ATLAS_WEB (default http://localhost:3000, or the
// staging URL in live mode), ATLAS_PLAYWRIGHT (path to the playwright package,
// default: resolved from the frontend's node_modules).
// Writes <out-dir>/shots/*.png (final screenshots), <out-dir>/frames/*.png and
// <out-dir>/frames/manifest.json (frame name + hold time in ms, used by make_gif.py).
// If a step fails, the page screenshot and text go to <out-dir>/debug/.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.ATLAS_PLAYWRIGHT ?? "playwright");

const LIVE = process.env.ATLAS_LIVE === "1";
const WEB =
  process.env.ATLAS_WEB ?? (LIVE ? "https://atlas-concur-ai.vercel.app" : "http://localhost:3000");
// Timeout scale: live model calls (planner, OCR, policy answers) take seconds.
const T = LIVE ? 4 : 1;
// Published synthetic demo password from the README / seed files.
const PASSWORD = "change-me-seed-password";
const GROUNDED_QUESTION = "What is the nightly hotel cap in New York?";
const OUT_OF_SCOPE_QUESTION = LIVE
  ? "What is the company stock price today?"
  : "What is the capital of France?";
const [outDir, receipt] = process.argv.slice(2);
const shotsDir = path.join(outDir, "shots");
const framesDir = path.join(outDir, "frames");
const debugDir = path.join(outDir, "debug");
fs.rmSync(framesDir, { recursive: true, force: true });
fs.mkdirSync(shotsDir, { recursive: true });
fs.mkdirSync(framesDir, { recursive: true });

const manifest = [];
let n = 0;
const settle = (page, ms = 700) => page.waitForTimeout(ms);
async function snap(page, name, hold = 1600) {
  const file = `${String(++n).padStart(3, "0")}-${name}.png`;
  await page.screenshot({ path: path.join(framesDir, file) });
  manifest.push({ file, hold });
}
async function shot(page, name) {
  await page.screenshot({ path: path.join(shotsDir, `${name}.png`) });
}
async function step(page, name, fn) {
  try {
    await fn();
  } catch (err) {
    fs.mkdirSync(debugDir, { recursive: true });
    await page.screenshot({ path: path.join(debugDir, `${name}.png`) }).catch(() => {});
    const text = await page.locator("body").innerText().catch(() => "");
    fs.writeFileSync(path.join(debugDir, `${name}.txt`), `${page.url()}\n\n${text}`);
    throw err;
  }
}

const browser = await chromium.launch();
async function login(role, { keepBanner = false } = {}) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    locale: "en-US",
    timezoneId: "UTC",
  });
  const page = await ctx.newPage();
  await page.goto(`${WEB}/login`);
  await page.getByLabel(/email/i).fill(`${role}@atlas-demo.internal`);
  await page.getByLabel(/^password/i).fill(PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL(/dashboard|approvals|analytics|expenses/, { timeout: 30000 * T });
  await page.waitForLoadState("networkidle").catch(() => {});
  await page.getByRole("button", { name: "Dismiss email verification notice" }).click().catch(() => {});
  await settle(page, 1200);
  // Staging shows a dismissible "Demo environment" notice (per browser session).
  if (!keepBanner) await dismissBanner(page);
  return { ctx, page };
}
async function dismissBanner(page) {
  const close = page.getByRole("button", { name: "Dismiss demo notice" });
  if (await close.count()) {
    await close.click();
    await settle(page, 400);
  }
}
async function chip(page, label, value, { numeric = false } = {}) {
  await page.getByRole("button", { name: new RegExp(`^(Add|Edit) ${label}$`) }).click();
  const input = page.getByRole(numeric ? "spinbutton" : "textbox", { name: `Edit ${label}`, exact: true });
  await input.fill(value);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await page.getByRole("button", { name: `Edit ${label}`, exact: true }).waitFor({ timeout: 30000 * T });
}
// Any rendered policy-answer state: a citation chip, an abstention or a caution.
const ANSWER = /§|Policy is silent|No confident answer|Needs review/;
async function askPolicy(page, question, name) {
  const answers = page.getByText(ANSWER);
  const before = await answers.count();
  const box = page.getByPlaceholder(/policy question/i);
  await box.fill(question);
  await snap(page, `${name}-typed`, 1500);
  await box.press("Enter");
  const deadline = Date.now() + 40000 * T;
  while ((await answers.count()) <= before) {
    if (Date.now() > deadline) throw new Error(`no policy answer for: ${question}`);
    await settle(page, 500);
  }
  await page.waitForLoadState("networkidle").catch(() => {});
  await settle(page, 1500);
}

// ---------------------------------------------------------------- employee
{
  const { ctx, page } = await login("employee", { keepBanner: LIVE });
  await snap(page, "employee-dashboard", 2200);
  await dismissBanner(page);

  // 1. Travel planner, chip-driven request SFO -> JFK, future date, 1 traveller.
  await step(page, "planner", async () => {
    await page.getByRole("link", { name: "Travel Planner" }).click();
    await page.waitForURL(/planner/);
    await settle(page, 1200);
    await snap(page, "planner-empty", 1200);
    await chip(page, "From", "SFO");
    await chip(page, "To", "JFK");
    await chip(page, "Depart", "2026-12-10");
    await snap(page, "planner-chips", 1200);
    await chip(page, "Travelers", "1", { numeric: true });
    const select = page.getByRole("button", { name: /^Select / }).first();
    await select.waitFor({ timeout: 60000 * T });
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 1800);
    await shot(page, "01-planner");
    await snap(page, "planner-options", 3200);
    await select.click();
    await page.getByText("Trip saved").first().waitFor({ timeout: 20000 * T });
    await settle(page, 900);
    await snap(page, "planner-selected", 2000);
  });

  // 2. Receipt upload with OCR extraction and per-field confidence.
  await step(page, "receipt", async () => {
    await page.getByRole("link", { name: "Receipts" }).click();
    await page.waitForURL(/receipts/);
    await settle(page, 1000);
    await snap(page, "receipts-empty", 1400);
    await page.locator("input[type=file]").first().setInputFiles(receipt);
    await page.getByText("Extracted fields").waitFor({ timeout: 30000 * T });
    await page.getByText("Ready to review").waitFor({ timeout: 30000 * T });
    await settle(page, 2200);
    await shot(page, "02-receipt-ocr");
    await snap(page, "receipt-extracted", 3600);
  });

  // 3. The draft report it created; submit it for approval.
  await step(page, "submit", async () => {
    await page.getByRole("link", { name: "Expenses" }).click();
    await page.waitForURL(/expenses/);
    await settle(page, 1000);
    await snap(page, "expenses-list", 1800);
    await page.getByRole("row", { name: /Receipt Inbox/ }).first().getByText("View", { exact: true }).click();
    await page.getByText("Pre-submit check").waitFor({ timeout: 20000 * T });
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 1200 * (LIVE ? 2 : 1));
    await snap(page, "report-presubmit", 3600);
    // A blocking policy flag (e.g. HOTEL-CAP on the policy-tripping receipt)
    // needs a justification before the report can be submitted.
    const justify = page.getByRole("button", { name: "Add justification" });
    for (let i = 0; i < 3 && (await justify.count()); i++) {
      await justify.first().click();
      const why = page.getByPlaceholder(/every capped property/);
      await why.waitFor({ timeout: 10000 * T });
      await why.fill("Client workshop on Park Avenue; every capped property nearby was sold out.");
      await settle(page, 600);
      await snap(page, "report-justify", 2400);
      await page.getByRole("button", { name: "Save changes" }).click();
      await page.getByRole("button", { name: "Edit justification" }).first().waitFor({ timeout: 20000 * T });
      await page.waitForLoadState("networkidle").catch(() => {});
      await settle(page, 1200);
    }
    const submit = page.locator('button[aria-disabled="false"]', { hasText: "Submit report" });
    await submit.waitFor({ timeout: 20000 * T });
    await submit.click();
    await settle(page, 1500);
    const confirm = page.getByRole("dialog").getByRole("button", { name: /submit/i });
    if (await confirm.count()) {
      await snap(page, "report-confirm", 1600);
      await confirm.last().click();
      await settle(page, 1500);
    }
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, LIVE ? 2500 : 0);
    await snap(page, "report-submitted", 2200);
  });
  await ctx.close();
}

// ----------------------------------------------------------------- manager
{
  const { ctx, page } = await login("manager");
  await step(page, "manager", async () => {
    await page.getByRole("link", { name: "Approvals" }).click();
    await page.waitForURL(/approvals/);
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 1500);
    await snap(page, "approvals-list", 1800);
    await page.getByText("Receipt Inbox").first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 2200);
    await snap(page, "approval-new-report", 3000);
    await page.getByText("Q3 Conference & Travel").first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 2500);
    await shot(page, "03-manager-approval");
    await snap(page, "approval-citation", 4200);
    // Decide the new report (approve), then show the list updated.
    await page.getByText("Receipt Inbox").first().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 1800);
    const approve = page.getByRole("button", { name: /^Approve/ }).first();
    await approve.scrollIntoViewIfNeeded();
    await snap(page, "approval-decide", 2200);
    await approve.click();
    await settle(page, 1200);
    const ok = page.getByRole("dialog").getByRole("button", { name: /approve|confirm/i });
    if (await ok.count()) await ok.last().click();
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 2200);
    await snap(page, "approval-approved", 2200);
  });
  await ctx.close();
}

// ----------------------------------------------------------------- finance
{
  const { ctx, page } = await login("finance");
  await step(page, "finance", async () => {
    await page.getByRole("link", { name: "Analytics" }).click();
    await page.waitForURL(/analytics/);
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 2500);
    await shot(page, "04-finance-analytics");
    await snap(page, "analytics", 4000);
    await page.mouse.wheel(0, 520);
    await settle(page, 1200);
    await snap(page, "analytics-scrolled", 2600);
    await page.getByRole("link", { name: "Audit" }).click();
    await page.waitForURL(/audit/);
    await page.waitForLoadState("networkidle").catch(() => {});
    await settle(page, 2000);
    await shot(page, "06-audit-trail");
    await snap(page, "audit-trail", 3200);
  });
  await ctx.close();
}

// ------------------------- policy Q&A: a grounded answer, then an abstention
{
  const { ctx, page } = await login("employee");
  await step(page, "policy", async () => {
    await page.getByRole("link", { name: "Travel Planner" }).click();
    await page.waitForURL(/planner/);
    await settle(page, 1000);
    const fresh = page.getByRole("button", { name: "New trip" });
    if (await fresh.count()) {
      await fresh.first().click();
      await settle(page, 800);
    }
    await page.getByRole("button", { name: "Ask Policy" }).click();
    await settle(page, 600);
    await snap(page, "policy-ask", 1500);
    // Local providers have no model, so this abstains too; README 05 needs LIVE.
    await askPolicy(page, GROUNDED_QUESTION, "policy-grounded");
    await shot(page, "05-policy-answer");
    await snap(page, "policy-answer", 5000);
    await askPolicy(page, OUT_OF_SCOPE_QUESTION, "policy-out-of-scope");
    await snap(page, "policy-abstains", 4500);
  });
  await ctx.close();
}

fs.writeFileSync(path.join(framesDir, "manifest.json"), JSON.stringify(manifest, null, 1));
await browser.close();
console.log(`frames: ${manifest.length}, total hold ${manifest.reduce((s, f) => s + f.hold, 0) / 1000}s`);
