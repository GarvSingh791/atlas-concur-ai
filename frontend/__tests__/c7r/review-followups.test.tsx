/**
 * C7R adversarial-review follow-ups (frontend):
 *   R1  needs-review receipts withhold the amount ONLY on amount evidence — a
 *       vendor-only reason must not blank a confidently-read total;
 *   R2  a blocking flag whose vendor ref matches several lines is not pinned on
 *       all of them ("Check exceptions"), and id refs stay exact;
 *   R3  entry_<idx> ordering compares instants, not ISO strings;
 *   R4  only transport failures (NetworkError / fetch-failure TypeErrors) and
 *       502/503/504 are retried — a TypeError bug in app code is not; 500 is
 *       not retried through the actual retry callback;
 *   R5  Playwright resolves the API URL the APP uses (.env files) and never
 *       reuses an uninspectable dev server while sensitive artifacts are on;
 *   R6  pins for behaviours the review showed untested (token reuse after a
 *       concurrent rotation, newer-version cache guard, changes-requested
 *       notice only on drafts).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  render,
  screen,
  cleanup,
  renderHook,
  waitFor,
} from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";

import { entryPolicyVerdicts } from "@/lib/approvals/entry-policy";
import { EntriesTable } from "@/components/expenses/entries-table";
import { ReceiptQueue } from "@/components/receipts/receipt-queue";
import { receiptAmountNeedsInput, toFieldRows } from "@/lib/receipts/fields";
import { isTransientError, retryTransient } from "@/lib/query/retry";
import { api, recoverFromUnauthorized } from "@/lib/api/client";
import {
  clearAllTokens,
  getAccessToken,
  setAccessToken,
  setRefreshToken,
} from "@/lib/auth/token-store";
import { ApiError, NetworkError } from "@/lib/types/api";
import type { Receipt } from "@/lib/types";
import type { RiskFlag } from "@/lib/types/approvals";
import type { ExpenseEntry } from "@/lib/types/expenses";
import {
  DEFAULT_API_URL,
  readDotenvValue,
  resolveAppApiUrl,
  resolveArtifactPolicy,
} from "@/playwright.artifact-policy";

afterEach(cleanup);

// ---------------------------------------------------------------------------
// R1 — receipts
// ---------------------------------------------------------------------------

function receipt(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rc-1",
    organization_id: "org",
    uploaded_by: "u",
    expense_id: null,
    image_url: "org/abc.png",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: {
      amount: 48.2,
      currency: "USD",
      date: "2026-01-02",
      vendor: null,
    },
    field_confidence: { amount: 0.99, currency: 0.95, date: 0.9 },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
    },
    created_at: "2026-01-02T10:00:00Z",
    needs_review: true,
    review_reasons: ["Vendor could not be read — enter it"],
    ...over,
  };
}

const amountRow = (r: Receipt) =>
  toFieldRows(r).find((x) => x.key === "amount")!;

describe("R1 needs-review withholds the amount only on amount evidence", () => {
  it("vendor-only reason: the confident total stays visible", () => {
    const r = receipt();
    expect(receiptAmountNeedsInput(r)).toBe(false);
    const row = amountRow(r);
    expect(row.requiresUserInput).toBe(false);
    expect(row.rawValue).toBe(48.2);
    expect(row.confidence).toBe(99);
  });

  it("queue label does not tell the user to enter a total that was read", () => {
    render(
      <ReceiptQueue
        items={[
          {
            id: "q1",
            fileName: "r.png",
            previewUrl: "",
            status: "processed",
            receipt: receipt(),
          } as never,
        ]}
        selectedId={null}
        onSelect={() => {}}
        onRetry={() => {}}
        onRemove={() => {}}
      />,
    );
    expect(screen.getByText("Needs review")).toBeTruthy();
    expect(screen.queryByText(/enter the total/)).toBeNull();
  });

  it.each<[string, Partial<Receipt>]>([
    [
      "pipeline review flag on amount (even at high confidence)",
      {
        extracted_fields: {
          amount: 13.37,
          currency: "USD",
          review_flags: { amount: "total_illegible" },
        },
        review_reasons: ["Total is not legible — enter the receipt total"],
      },
    ],
    [
      "amount missing",
      {
        extracted_fields: { amount: null, currency: "USD" },
        review_reasons: ["Amount could not be read — enter the receipt total"],
      },
    ],
    [
      "amount confidence below 0.70",
      { field_confidence: { amount: 0.41 }, review_reasons: [] },
    ],
    ["amount confidence absent", { field_confidence: {}, review_reasons: [] }],
    [
      "amount/total reason text only",
      {
        review_reasons: [
          "Amount was not read with confidence — enter the receipt total",
        ],
      },
    ],
    [
      "OCR failed",
      {
        ocr_status: "failed",
        review_reasons: [
          "Receipt could not be read — enter the details manually",
        ],
      },
    ],
  ])("withholds the amount: %s", (_, over) => {
    const r = receipt(over);
    expect(receiptAmountNeedsInput(r)).toBe(true);
    const row = amountRow(r);
    expect(row.requiresUserInput).toBe(true);
    expect(row.rawValue).toBeNull();
  });

  it("a user-entered amount is never withheld", () => {
    const r = receipt({
      field_confidence: { amount: 0.2 },
      field_provenance: { amount: "user_edited" },
    });
    expect(receiptAmountNeedsInput(r)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// R2/R3 — approval line-item policy attribution
// ---------------------------------------------------------------------------

function entry(over: Partial<ExpenseEntry>): ExpenseEntry {
  return {
    id: "e-x",
    organization_id: "org",
    report_id: "r1",
    trip_segment_id: null,
    category: "lodging",
    amount: "100.00",
    currency: "USD",
    expense_date: "2026-06-11",
    vendor: null,
    description: null,
    status: "draft",
    match_source: "manual",
    is_itemized: false,
    created_by: "u",
    updated_by: "u",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    receipt_id: null,
    ...over,
  };
}

const HYATT_A = entry({ id: "a", vendor: "Grand Hyatt", amount: "180.00" });
const HYATT_B = entry({ id: "b", vendor: "Grand Hyatt", amount: "420.00" });
const CAP: RiskFlag = {
  rule_id: "HOTEL-CAP",
  entry_ref: "Grand Hyatt",
  severity: "blocking",
};

describe("R2 ambiguous vendor refs", () => {
  it("a blocking vendor flag shared by two lines marks neither Out of policy", () => {
    const v = entryPolicyVerdicts([HYATT_A, HYATT_B], [CAP]);
    expect(v.get("a")).toBe("ambiguous");
    expect(v.get("b")).toBe("ambiguous");
  });

  it("renders 'Check exceptions' — neither 'Out of policy' nor 'In policy'", () => {
    render(
      <EntriesTable
        entries={[HYATT_A, HYATT_B]}
        policyVerdicts={entryPolicyVerdicts([HYATT_A, HYATT_B], [CAP])}
      />,
    );
    expect(screen.getAllByText("Check exceptions")).toHaveLength(2);
    expect(screen.queryByText("Out of policy")).toBeNull();
    expect(screen.queryByText("In policy")).toBeNull();
  });

  it("an id ref is exact even when vendors collide; status 'flagged' still wins", () => {
    const flags = [{ ...CAP, entry_ref: null, entry_id: "b" } as RiskFlag];
    const v = entryPolicyVerdicts([HYATT_A, HYATT_B], flags);
    expect(v.get("a")).toBe("none");
    expect(v.get("b")).toBe("blocking");
  });

  it("a unique vendor match is still blocking; advisory stays advisory", () => {
    const solo = entryPolicyVerdicts([HYATT_A], [CAP]);
    expect(solo.get("a")).toBe("blocking");
    const adv = entryPolicyVerdicts(
      [HYATT_A, HYATT_B],
      [{ ...CAP, severity: "advisory" }],
    );
    expect(adv.get("a")).toBe("advisory");
    expect(adv.get("b")).toBe("advisory");
  });

  it("an exact blocking ref on one line outranks an ambiguous one", () => {
    const v = entryPolicyVerdicts(
      [HYATT_A, HYATT_B],
      [CAP, { rule_id: "X", entry_ref: "entry_b", severity: "blocking" }],
    );
    expect(v.get("a")).toBe("ambiguous");
    expect(v.get("b")).toBe("blocking");
  });
});

describe("R3 entry_<idx> follows (created_at, id) by instant", () => {
  it("'…:00Z' (whole second) precedes '…:00.250000Z' of the same second", () => {
    // String order would put the fractional one first ('.' < 'Z').
    const whole = entry({ id: "z-whole", created_at: "2026-07-01T10:00:00Z" });
    const frac = entry({
      id: "a-frac",
      created_at: "2026-07-01T10:00:00.250000Z",
    });
    const flag: RiskFlag = { entry_ref: "entry_0", severity: "blocking" };
    const v = entryPolicyVerdicts([frac, whole], [flag]);
    expect(v.get("z-whole")).toBe("blocking");
    expect(v.get("a-frac")).toBe("none");
  });

  it("sub-millisecond digits break same-millisecond ties before the id", () => {
    const later = entry({ id: "a", created_at: "2026-07-01T10:00:00.000900Z" });
    const earlier = entry({
      id: "b",
      created_at: "2026-07-01T10:00:00.000100Z",
    });
    const v = entryPolicyVerdicts(
      [later, earlier],
      [{ entry_ref: "entry_0", severity: "blocking" }],
    );
    expect(v.get("b")).toBe("blocking");
    expect(v.get("a")).toBe("none");
  });
});

// ---------------------------------------------------------------------------
// R4 — retry classification
// ---------------------------------------------------------------------------

describe("R4 transient retry only for transport failures", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("the API client turns a fetch rejection into a NetworkError (retried)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const err = await api.get("/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(NetworkError);
    expect(err).toBeInstanceOf(TypeError); // existing checks keep working
    expect(isTransientError(err)).toBe(true);
    expect(retryTransient(0, err)).toBe(true);
  });

  it("a TypeError bug in app code is NOT retried", () => {
    const bug = new TypeError(
      "Cannot read properties of undefined (reading 'items')",
    );
    expect(isTransientError(bug)).toBe(false);
    expect(retryTransient(0, bug)).toBe(false);
  });

  it("raw fetch transport messages from each engine are still recognised", () => {
    for (const m of [
      "Failed to fetch",
      "NetworkError when attempting to fetch resource.",
      "Load failed",
      "fetch failed",
    ]) {
      expect(isTransientError(new TypeError(m))).toBe(true);
    }
  });

  it("the retry callback never retries 500 or 4xx, does retry 503", () => {
    const e = (status: number) =>
      new ApiError({ code: "X", message: "x", correlation_id: "c" }, status);
    expect(retryTransient(0, e(500))).toBe(false);
    expect(retryTransient(0, e(404))).toBe(false);
    expect(retryTransient(0, e(503))).toBe(true);
    expect(retryTransient(3, e(503))).toBe(false); // capped
  });

  it("useQuery with the policy calls a buggy queryFn exactly once", async () => {
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: retryTransient, retryDelay: 1 } },
    });
    const fn = vi.fn(() => {
      throw new TypeError("x is not a function");
    });
    const { result } = renderHook(
      () => useQuery({ queryKey: ["bug"], queryFn: fn }),
      {
        wrapper: ({ children }) => (
          <QueryClientProvider client={qc}>{children}</QueryClientProvider>
        ),
      },
    );
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(fn).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// R5 — Playwright API locality
// ---------------------------------------------------------------------------

describe("R5 Playwright checks the API URL the app will use", () => {
  const files = (map: Record<string, string>) => (name: string) =>
    map[name] ?? null;

  it("a remote URL that exists only in .env.local is seen (and blocks artifacts)", () => {
    const api = resolveAppApiUrl(
      {},
      files({
        ".env.local":
          "# staging\nNEXT_PUBLIC_API_BASE_URL=https://atlas-backend.example.com/api/v1\n",
      }),
    );
    expect(api).toBe("https://atlas-backend.example.com/api/v1");
    const p = resolveArtifactPolicy(
      { E2E_HTML_REPORT: "1", E2E_TRACE: "on" },
      "http://localhost:3000",
      api,
    );
    expect(p.reporter).toEqual([["list"]]);
    expect(p.trace).toBe("off");
  });

  it("follows next dev precedence: shell > .env.development.local > .env.local > .env", () => {
    const f = files({
      ".env.development.local":
        "NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:9000",
      ".env.local": "NEXT_PUBLIC_API_BASE_URL=https://remote.example.com",
      ".env": "NEXT_PUBLIC_API_BASE_URL=https://other.example.com",
    });
    expect(resolveAppApiUrl({}, f)).toBe("http://127.0.0.1:9000");
    expect(
      resolveAppApiUrl({ NEXT_PUBLIC_API_BASE_URL: "http://localhost:1" }, f),
    ).toBe("http://localhost:1");
    expect(resolveAppApiUrl({}, files({}))).toBe(DEFAULT_API_URL);
  });

  it("parses export/quotes/comments and fails closed on $VAR expansion", () => {
    expect(
      readDotenvValue(
        'export NEXT_PUBLIC_API_BASE_URL="https://q.example.com/api" # c',
        "NEXT_PUBLIC_API_BASE_URL",
      ),
    ).toBe("https://q.example.com/api");
    expect(
      readDotenvValue(
        "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 # local",
        "NEXT_PUBLIC_API_BASE_URL",
      ),
    ).toBe("http://localhost:8000");
    const expanded = resolveAppApiUrl(
      {},
      files({ ".env.local": "NEXT_PUBLIC_API_BASE_URL=$API_HOST/api/v1" }),
    );
    expect(expanded).toBe("");
    expect(
      resolveArtifactPolicy(
        { E2E_HTML_REPORT: "1" },
        "http://localhost:3000",
        expanded,
      ).reporter,
    ).toEqual([["list"]]);
  });

  it("sensitive artifacts without the override forbid reusing a running dev server", () => {
    const local = "http://localhost:8000/api/v1";
    const app = "http://localhost:3000";
    expect(resolveArtifactPolicy({}, app, local).mayReuseServer).toBe(true);
    const on = resolveArtifactPolicy({ E2E_TRACE: "on" }, app, local);
    expect(on.sensitive).toBe(true);
    expect(on.mayReuseServer).toBe(false);
    expect(
      resolveArtifactPolicy(
        { E2E_HTML_REPORT: "1", E2E_ALLOW_SENSITIVE_ARTIFACTS: "1" },
        app,
        local,
      ).mayReuseServer,
    ).toBe(true);
  });

  it("the committed config resolves the API from .env files and gates reuse", () => {
    const src = readFileSync(
      path.resolve(__dirname, "../../playwright.config.ts"),
      "utf8",
    );
    expect(src).toMatch(/resolveAppApiUrl\(/);
    expect(src).toMatch(/reuseExistingServer:[^\n]*ARTIFACTS\.mayReuseServer/);
    expect(src).not.toMatch(/process\.env\.NEXT_PUBLIC_API_BASE_URL \?\?/);
  });
});

// ---------------------------------------------------------------------------
// R6 — pins for previously untested decisions
// ---------------------------------------------------------------------------

describe("R6 recoverFromUnauthorized reuses a token rotated by another caller", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    localStorage.clear();
    clearAllTokens();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("no second /auth/refresh when the token already changed", async () => {
    setRefreshToken("rt-1");
    setAccessToken("at-new"); // rotated while our request (sent with at-old) was in flight
    await expect(recoverFromUnauthorized("at-old")).resolves.toBe("at-new");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(getAccessToken()).toBe("at-new");
  });
});
