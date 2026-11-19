/**
 * C7R issue 2 — the approval detail's line-item table must agree with the
 * approval's risk flags (Policy column) and with the entry's linked receipt /
 * match_source (Match column). Staging showed "In policy" + "Manual" for a
 * HOTEL-CAP blocking line that had a receipt attached.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));

import { DecisionPanel } from "@/components/approvals/decision-panel";
import { entryPolicyVerdicts } from "@/lib/approvals/entry-policy";
import { matchPresentation } from "@/lib/expenses/format";
import type { ApprovalDetail, RiskFlag } from "@/lib/types/approvals";
import type { ExpenseEntry } from "@/lib/types/expenses";

afterEach(cleanup);

function entry(over: Partial<ExpenseEntry>): ExpenseEntry {
  return {
    id: "e-x",
    organization_id: "org",
    report_id: "r1",
    trip_segment_id: null,
    category: "meals",
    amount: "40.00",
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

const HOTEL = entry({
  id: "e-hotel",
  category: "lodging",
  vendor: "Grand Hyatt",
  amount: "612.00",
  match_source: "manual",
  receipt_id: "rcpt-1",
  status: "draft", // the entry row itself is NOT marked flagged
  created_at: "2026-06-11T09:00:00Z",
});
const LUNCH = entry({
  id: "e-lunch",
  vendor: "Corner Cafe",
  amount: "18.00",
  match_source: "manual",
  created_at: "2026-06-11T10:00:00Z",
});

const HOTEL_CAP: RiskFlag = {
  entry_ref: "Grand Hyatt",
  flag_type: "policy_violation",
  rule_id: "HOTEL-CAP",
  rule_type: "hotel_cap",
  source_clause_ref: "LODG-02",
  reason: "Nightly rate $612 exceeds the $300 cap.",
  severity: "blocking",
};

function detail(flags: RiskFlag[], entries: ExpenseEntry[]): ApprovalDetail {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u-sub",
    submitter_name: "Alex",
    current_status: "pending",
    is_auto_approved: false,
    risk_summary: {
      route: "escalate",
      subject_in_policy: false,
      blocking_flag_count: flags.filter((f) => f.severity === "blocking")
        .length,
      flags,
    },
    risk_level: "high",
    version: 1,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    steps: [],
    expense_report: {
      report: {
        id: "r1",
        organization_id: "org",
        owner_id: "u-sub",
        trip_id: null,
        title: "NYC trip",
        status: "submitted",
        total_amount: "630.00",
        currency: "USD",
        submitted_at: "2026-06-12T00:00:00Z",
        pre_submit_flags: null,
        created_by: null,
        updated_by: null,
        created_at: "2026-06-12T00:00:00Z",
        updated_at: "2026-06-12T00:00:00Z",
        version: 1,
      },
      entries,
    },
    trip: null,
  };
}

function renderPanel(d: ApprovalDetail) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DecisionPanel
        approvalId={d.id}
        detail={d}
        isLoading={false}
        isError={false}
        onRetry={() => {}}
        onBack={() => {}}
        canDecide
        currentUserId="u-mgr"
      />
    </QueryClientProvider>,
  );
}

function rowFor(text: string): HTMLElement {
  // The vendor also appears in the Exceptions list — pick the table row.
  const rows = screen
    .getAllByText(text)
    .map((el) => el.closest("tr"))
    .filter((r): r is HTMLTableRowElement => r !== null);
  if (rows.length !== 1) throw new Error(`expected one row for ${text}`);
  return rows[0];
}

describe("C7R-2 approval detail line items", () => {
  it("a blocking flag makes its line 'Out of policy'; a linked receipt is not 'Manual'", () => {
    renderPanel(detail([HOTEL_CAP], [HOTEL, LUNCH]));
    const hotel = within(rowFor("Grand Hyatt"));
    expect(hotel.getByText("Out of policy")).toBeTruthy();
    expect(hotel.queryByText("In policy")).toBeNull();
    expect(hotel.getByText("Manual · Receipt")).toBeTruthy();
    expect(hotel.queryByText(/^Manual$/)).toBeNull();

    const lunch = within(rowFor("Corner Cafe"));
    expect(lunch.getByText("In policy")).toBeTruthy();
    expect(lunch.getByText("Manual")).toBeTruthy();
  });

  it("advisory-only flags show an amber 'Advisory', not a violation", () => {
    renderPanel(
      detail(
        // A real advisory flag type: a policy_violation is always an exception
        // for the approver, even when justified ("advisory" for submit only).
        [
          {
            ...HOTEL_CAP,
            flag_type: "approval_required",
            entry_ref: "Corner Cafe",
            severity: "advisory",
          },
        ],
        [HOTEL, LUNCH],
      ),
    );
    expect(within(rowFor("Corner Cafe")).getByText("Advisory")).toBeTruthy();
    expect(within(rowFor("Grand Hyatt")).getByText("In policy")).toBeTruthy();
  });

  it("a JUSTIFIED HOTEL-CAP violation stays an exception and shows the claimant's justification", () => {
    // C7R UAT: the claimant's justification made this flag "advisory" so the
    // report could be submitted — for the approver it is still out of policy.
    const justified: RiskFlag = {
      ...HOTEL_CAP,
      entry_id: "e-hotel",
      severity: "advisory",
      justifiable: true,
      justification:
        "Conference week — every hotel near the venue was over cap.",
    };
    const { container } = renderPanel(detail([justified], [HOTEL, LUNCH]));

    const hotel = within(rowFor("Grand Hyatt"));
    expect(hotel.getByText("Out of policy")).toBeTruthy();
    expect(hotel.queryByText("Advisory")).toBeNull();
    expect(within(rowFor("Corner Cafe")).getByText("In policy")).toBeTruthy();

    // Exceptions list: the claimant's reason is visible without expanding the row.
    expect(screen.getByText("Claimant justification")).toBeTruthy();
    expect(
      screen.getByText(
        "“Conference week — every hotel near the venue was over cap.”",
      ),
    ).toBeTruthy();
    // ...and the exception is styled as a violation (error), not a warning.
    const reason = screen.getByText("Nightly rate $612 exceeds the $300 cap.");
    const card = reason.parentElement as HTMLElement;
    const icon = card.querySelector("svg");
    expect(icon?.getAttribute("class")).toContain("color-error");
    expect(container.textContent).not.toContain("undefined");
  });
});

describe("entryPolicyVerdicts ref matching", () => {
  const SEG = entry({
    id: "e-seg",
    trip_segment_id: "seg-9",
    vendor: "Delta",
    created_at: "2026-06-11T08:00:00Z",
  });
  const NOVENDOR_A = entry({ id: "e-a", created_at: "2026-06-11T11:00:00Z" });
  const NOVENDOR_B = entry({ id: "e-b", created_at: "2026-06-11T12:00:00Z" });
  const all = [NOVENDOR_B, SEG, NOVENDOR_A, HOTEL]; // API order ≠ eval order

  const verdict = (flag: RiskFlag) => entryPolicyVerdicts(all, [flag]);

  it("matches by entry id and entry_<id>", () => {
    expect(verdict({ ...HOTEL_CAP, entry_ref: "e-a" }).get("e-a")).toBe(
      "blocking",
    );
    expect(verdict({ ...HOTEL_CAP, entry_ref: "entry_e-b" }).get("e-b")).toBe(
      "blocking",
    );
  });

  it("matches segment_<id> and segment_id to the entry's trip segment", () => {
    expect(
      verdict({ ...HOTEL_CAP, entry_ref: "segment_seg-9" }).get("e-seg"),
    ).toBe("blocking");
    expect(
      verdict({ segment_id: "seg-9", in_policy: false }).get("e-seg"),
    ).toBe("blocking");
  });

  it("matches vendor refs case-insensitively", () => {
    expect(
      verdict({ ...HOTEL_CAP, entry_ref: "grand hyatt " }).get("e-hotel"),
    ).toBe("blocking");
  });

  it("matches entry_<idx> by evaluation order (created_at, id) for vendor-less entries", () => {
    // eval order: SEG(08:00)=0, HOTEL(09:00)=1, A(11:00)=2, B(12:00)=3
    const v = verdict({ ...HOTEL_CAP, entry_ref: "entry_2" });
    expect(v.get("e-a")).toBe("blocking");
    expect(v.get("e-b")).toBe("none");
    // an index never matches an entry that HAS a vendor (ref would be vendor)
    expect(verdict({ ...HOTEL_CAP, entry_ref: "entry_1" }).get("e-hotel")).toBe(
      "none",
    );
  });

  it("a flag carrying entry_id attributes to that entry only (same-vendor lines)", () => {
    // C7R review: the vendor fallback also matched the OTHER same-vendor line,
    // so both rows showed "Check exceptions". Old: both 'ambiguous'.
    const h1 = entry({
      id: "h-1",
      vendor: "Hilton",
      created_at: "2026-06-11T01:00:00Z",
    });
    const h2 = entry({
      id: "h-2",
      vendor: "Hilton",
      created_at: "2026-06-11T02:00:00Z",
    });
    const flag = {
      ...HOTEL_CAP,
      entry_ref: "Hilton",
      entry_id: "h-1",
    } as RiskFlag;
    const v = entryPolicyVerdicts([h1, h2], [flag]);
    expect(v.get("h-1")).toBe("blocking");
    expect(v.get("h-2")).toBe("none");
    // An entry_id that matches no row does not fall back to the vendor either.
    const stale = {
      ...HOTEL_CAP,
      entry_ref: "Hilton",
      entry_id: "gone",
    } as RiskFlag;
    const v2 = entryPolicyVerdicts([h1, h2], [stale]);
    expect(v2.get("h-1")).toBe("none");
    expect(v2.get("h-2")).toBe("none");
  });

  it("in-policy trip flags and unrelated refs leave entries clean", () => {
    const v = entryPolicyVerdicts(all, [
      { segment_id: "seg-9", in_policy: true },
      { ...HOTEL_CAP, entry_ref: "Somewhere else" },
    ]);
    expect(Array.from(v.values()).every((x) => x === "none")).toBe(true);
    expect(entryPolicyVerdicts(all, null).get("e-a")).toBe("none");
  });
});

describe("matchPresentation honours receipt_id", () => {
  it("labels by match_source first, then by a linked receipt", () => {
    expect(matchPresentation("receipt", "r").label).toBe("Auto · Receipt");
    expect(matchPresentation("card_txn", null).label).toBe("Auto · Card");
    expect(matchPresentation("manual", "r").label).toBe("Manual · Receipt");
    expect(matchPresentation("manual", null).label).toBe("Manual");
    expect(matchPresentation(null, "r").label).toBe("Receipt linked");
    expect(matchPresentation(null, null).label).toBe("Unmatched");
  });
});
