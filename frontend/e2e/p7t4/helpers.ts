import {
  type APIRequestContext,
  type Page,
  request as playwrightRequest,
  expect,
} from "@playwright/test";
import { EMAIL, PASSWORD, navTo, type Role } from "../helpers";

/**
 * P7-T4 shared helpers.
 *
 * Two layers:
 *   - API helpers (apiLogin / seedDraftReport / submitReport / findApproval /
 *     seedTrip / submitTrip) — used to LOG IN once per role and to SELF-SEED the mutating
 *     fixtures each mutating spec needs, so the suite is idempotent across
 *     re-runs (a decided approval / submitted report can't be re-driven).
 *   - UI helpers (addChip) — encapsulate the planner's Add→Edit→Save chip dance
 *     with web-first auto-waiting (no sleeps).
 */

// The frontend reads NEXT_PUBLIC_API_BASE_URL (default :8000/api/v1); the tests
// talk to the same live backend directly for seeding.
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export { EMAIL, PASSWORD, navTo };
export type { Role };
export { expect };

// localStorage key the app uses for the refresh token (token-store.ts).
export const RT_KEY = "atlas_rt";

/**
 * Boot the authenticated app shell for a storageState-backed test.
 *
 * storageState only seeds localStorage["atlas_rt"] — it does NOT load a page, so
 * a test starts on about:blank with no session mounted. We must load an app
 * route first; SessionProvider.hydrateSession then reads atlas_rt → refresh →
 * /me → renders the shell (the sidebar nav). navTo() can only run once that nav
 * exists, so every authenticated spec calls bootApp() before its first navTo().
 */
export async function bootApp(page: Page, href = "/dashboard"): Promise<void> {
  await page.goto(href, { waitUntil: "domcontentloaded" });
  await page
    .locator('nav[aria-label="Main Navigation"]')
    .waitFor({ state: "visible", timeout: 60_000 });
}

export interface Tokens {
  access_token: string;
  refresh_token: string;
}

/**
 * API login for a role → returns both tokens. Faithful to the app's real auth:
 * the refresh token is what storageState seeds into localStorage["atlas_rt"],
 * and the app hydrates a session from it on mount.
 */
export async function apiLogin(
  ctx: APIRequestContext,
  role: Role,
): Promise<Tokens> {
  const res = await ctx.post(`${API_BASE}/auth/login`, {
    data: { email: EMAIL[role], password: PASSWORD },
  });
  expect(
    res.ok(),
    `login for ${role} failed: ${res.status()} ${await res.text()}`,
  ).toBeTruthy();
  const body = (await res.json()) as Tokens;
  expect(body.access_token, "login returned an access_token").toBeTruthy();
  expect(body.refresh_token, "login returned a refresh_token").toBeTruthy();
  return body;
}

/** Standalone request context + login in one call (for use inside specs). */
export async function apiLoginStandalone(role: Role): Promise<{
  ctx: APIRequestContext;
  tokens: Tokens;
}> {
  const ctx = await playwrightRequest.newContext();
  const tokens = await apiLogin(ctx, role);
  return { ctx, tokens };
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` };
}

export interface SeededReport {
  id: string;
  title: string;
  total_amount: string;
  status: string;
}

/**
 * Create a draft expense report whose total is > $500 so the approval gate
 * deterministically ESCALATES to a real pending item (it auto-approves only when
 * total ≤ $500 AND in-policy AND no blocking flags). total_amount is derived
 * server-side from the entries — never accepted from the client — so we attach
 * an airfare entry of $750.
 */
export async function seedDraftReport(
  accessToken: string,
  title: string,
): Promise<SeededReport> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.post(`${API_BASE}/expense-reports`, {
      headers: authHeaders(accessToken),
      data: {
        title,
        currency: "USD",
        entries: [
          {
            category: "airfare",
            amount: "750.00",
            currency: "USD",
            expense_date: "2026-06-10",
            vendor: "United Airlines",
            description: "E2E self-seeded airfare (> $500 → escalates)",
          },
        ],
      },
    });
    expect(
      res.status(),
      `create report failed: ${res.status()} ${await res.text()}`,
    ).toBe(201);
    const body = (await res.json()) as SeededReport;
    expect(
      Number(body.total_amount),
      "seeded report total must exceed $500 to escalate",
    ).toBeGreaterThan(500);
    return body;
  } finally {
    await ctx.dispose();
  }
}

/** Transition a draft report → submitted via the API (cross-role bridge). */
export async function submitReport(
  accessToken: string,
  reportId: string,
): Promise<void> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.patch(`${API_BASE}/expense-reports/${reportId}`, {
      headers: authHeaders(accessToken),
      data: { status: "submitted" },
    });
    expect(
      res.ok(),
      `submit report failed: ${res.status()} ${await res.text()}`,
    ).toBeTruthy();
  } finally {
    await ctx.dispose();
  }
}

/** Create a draft trip (> default cap) so a trip approval escalates to pending. */
export async function seedTrip(
  accessToken: string,
  purpose: string,
): Promise<{ id: string }> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.post(`${API_BASE}/trips`, {
      headers: authHeaders(accessToken),
      data: {
        origin: "SFO",
        primary_destination: "JFK",
        start_date: "2026-06-10",
        end_date: "2026-06-12",
        currency: "USD",
        purpose,
        estimated_cost: "1200.00",
      },
    });
    expect(
      res.status(),
      `create trip failed: ${res.status()} ${await res.text()}`,
    ).toBe(201);
    return (await res.json()) as { id: string };
  } finally {
    await ctx.dispose();
  }
}

/**
 * Submit a draft trip for approval (draft → pending_approval) as its OWNER via
 * PATCH /trips/{id}. C7-13: the backend only opens an approval on a SUBMITTED
 * subject (trip == pending_approval) — the submit itself opens it (C7R).
 */
export async function submitTrip(
  accessToken: string,
  tripId: string,
): Promise<void> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.patch(`${API_BASE}/trips/${tripId}`, {
      headers: authHeaders(accessToken),
      data: { status: "pending_approval" },
    });
    expect(
      res.status(),
      `submit trip failed: ${res.status()} ${await res.text()}`,
    ).toBe(200);
    const body = (await res.json()) as { status?: string };
    expect(body.status).toBe("pending_approval");
  } finally {
    await ctx.dispose();
  }
}

/**
 * Find the approval the owner's SUBMIT opened for a subject, using an APPROVER
 * (manager/finance) token. Submitting a report (→ submitted) or a trip
 * (→ pending_approval) runs the Approval Agent in the same transaction (C7R),
 * so no POST /approvals is needed — a POST now returns 409 (already exists).
 * Looks the subject up in the approver's queue (newest first). Returns the
 * approval id and whether it auto-approved.
 */
export async function findApproval(
  approverAccessToken: string,
  subjectType: "trip" | "expense_report",
  subjectId: string,
): Promise<{ id: string; current_status: string; is_auto_approved: boolean }> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.get(`${API_BASE}/approvals`, {
      headers: authHeaders(approverAccessToken),
      params: { subject_type: subjectType, sort: "created_at:desc", size: 100 },
    });
    expect(
      res.ok(),
      `list approvals failed: ${res.status()} ${await res.text()}`,
    ).toBeTruthy();
    const body = (await res.json()) as {
      items: {
        id: string;
        subject_id: string;
        current_status: string;
        is_auto_approved: boolean;
      }[];
    };
    const approval = body.items.find((a) => a.subject_id === subjectId);
    expect(
      approval,
      `no approval was opened on submit for ${subjectType} ${subjectId}`,
    ).toBeTruthy();
    return approval!;
  } finally {
    await ctx.dispose();
  }
}

/** GET /approvals/{id} — the server's truth for a decision (T2). */
export async function getApproval(
  accessToken: string,
  approvalId: string,
): Promise<{ id: string; current_status: string }> {
  const ctx = await playwrightRequest.newContext();
  try {
    const res = await ctx.get(`${API_BASE}/approvals/${approvalId}`, {
      headers: authHeaders(accessToken),
    });
    expect(
      res.ok(),
      `get approval failed: ${res.status()} ${await res.text()}`,
    ).toBeTruthy();
    return (await res.json()) as { id: string; current_status: string };
  } finally {
    await ctx.dispose();
  }
}

/**
 * Approve ONE specific approval through the UI and prove it really happened
 * (T2/T3). Targets the queue row by its approval id (data-approval-id) — never
 * `.first()` on a reusable label — waits for THAT row's PATCH /approvals/{id}
 * response (so closing the page/context afterwards can't abort it), asserts the
 * server returned "approved", and checks the row's own status badge. A
 * page-wide getByText(/^Approved$/) can match pre-existing badges and toasts,
 * so it is not used as evidence.
 */
export async function approveInUi(
  page: Page,
  approvalId: string,
): Promise<void> {
  const row = page.locator(`[data-approval-id="${approvalId}"]`);
  await row.waitFor({ state: "visible", timeout: 30_000 });
  await row.click();

  const patch = page.waitForResponse(
    (res) =>
      res.request().method() === "PATCH" &&
      new URL(res.url()).pathname.endsWith(`/approvals/${approvalId}`),
    { timeout: 30_000 },
  );
  await page.getByRole("button", { name: /^Approve/i }).click();
  const res = await patch;
  expect(res.status(), `PATCH /approvals/${approvalId}`).toBe(200);
  const body = (await res.json()) as { id: string; current_status: string };
  expect(body.id).toBe(approvalId);
  expect(body.current_status).toBe("approved");

  // The SAME row re-renders with the decided status after the queue refetch.
  await expect(row.getByText(/^Approved$/)).toBeVisible({ timeout: 20_000 });
}

/**
 * Drive the planner's chip editor: click "Add {label}" (or "Edit {label}" if the
 * chip is already filled), type the value into the editor input, and Save.
 *
 * Each Save re-streams the plan; chips are `disabled` while streaming. We rely
 * on Playwright's actionability auto-waiting (it waits for the control to be
 * enabled) instead of any sleep.
 */
export async function addChip(
  page: Page,
  label: "From" | "To" | "Depart" | "Travelers",
  value: string,
): Promise<void> {
  // One opener locator covering BOTH chip states: "Add {label}" when empty,
  // "Edit {label}" once it has a value (the chip persists across re-streams /
  // sessionStorage hydration, so either may be the live state). A single locator
  // lets Playwright's actionability auto-waiting handle the disabled-while-
  // streaming window — no point-in-time count() race, no sleeps.
  const opener = page.getByRole("button", {
    name: new RegExp(`^(Add|Edit) ${label}$`),
  });
  await opener.click();

  // The editor input is a textbox (From/To/Depart) or spinbutton (Travelers);
  // Depart is a type=date input whose role is still "textbox" with an aria-label
  // accessible name. Fill the value directly.
  const input = page.getByRole(
    label === "Travelers" ? "spinbutton" : "textbox",
    { name: `Edit ${label}`, exact: true },
  );
  await input.waitFor({ state: "visible" });
  await input.fill(value);
  await page.getByRole("button", { name: "Save", exact: true }).click();

  // Save re-streams the plan and collapses the editor back into a filled chip.
  // Assert the value round-tripped: the editor input is gone and the chip now
  // offers "Edit {label}" (present even while disabled during the re-stream).
  await expect(input).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: `Edit ${label}`, exact: true }),
  ).toBeVisible();
}
