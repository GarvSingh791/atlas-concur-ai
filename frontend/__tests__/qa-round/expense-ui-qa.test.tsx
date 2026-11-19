/**
 * QA round, defect 8 — expense screens.
 *
 *  - New report "Trip" dropdown: four trips read "ORD → JFK (2026-11-17)" — now
 *    each option carries title / dates / status, and identical ones are told
 *    apart by when they were created (then a counter).
 *  - The EXP-04 affidavit checkbox unticks the moment the amount, date, vendor
 *    or description changes (the server withdraws the signature on save anyway).
 *  - A reimbursed (or approved) report no longer shows the pre-submit notice
 *    "This will require approver sign-off after submission".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const h = vi.hoisted(() => ({
  updateMutate: vi.fn(),
  useExpenseReport: vi.fn(),
  useReportEntries: vi.fn(),
}));
vi.mock("@/lib/query/expense-hooks", () => ({
  useUpdateEntry: () => ({ mutate: h.updateMutate, isPending: false }),
  useExpenseReport: h.useExpenseReport,
  useReportEntries: h.useReportEntries,
  useExpenseReports: vi.fn(),
  useSubmitReport: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ user: { id: "emp" }, hasPermission: () => true }),
}));

import { EntryEditor } from "@/components/expenses/entry-editor";
import { ReportDetail } from "@/components/expenses/report-detail";
import { tripOptionLabels } from "@/lib/expenses/trip-options";
import type { ReadinessFlag } from "@/lib/expenses/readiness";
import type { ExpenseEntry, ExpenseReport } from "@/lib/types";
import type { TripListItem } from "@/lib/types/trips";

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

function trip(over: Partial<TripListItem>): TripListItem {
  return {
    id: "t",
    organization_id: "o",
    traveler_id: "emp",
    origin: "ORD",
    primary_destination: "JFK",
    start_date: "2026-11-17",
    end_date: "2026-11-19",
    currency: "USD",
    status: "draft",
    version: 1,
    created_at: "2026-11-01T10:00:00Z",
    updated_at: "2026-11-01T10:00:00Z",
    ...over,
  };
}

describe("New report trip options", () => {
  it("labels trips with title, dates and status", () => {
    const labels = tripOptionLabels([
      trip({ id: "a", purpose: "Client summit", status: "approved" }),
      trip({ id: "b", end_date: "2026-11-20" }),
    ]);
    expect(labels.get("a")).toBe(
      "ORD → JFK · 2026-11-17 – 2026-11-19 · Client summit · Approved",
    );
    expect(labels.get("b")).toBe("ORD → JFK · 2026-11-17 – 2026-11-20 · Draft");
  });

  it("never shows two identical options", () => {
    const trips = [
      trip({ id: "a", created_at: "2026-11-01T10:00:00Z" }),
      trip({ id: "b", created_at: "2026-11-02T10:00:00Z" }),
      trip({ id: "c", created_at: "2026-11-02T11:00:00Z" }),
      trip({ id: "d", created_at: "2026-11-02T12:00:00Z" }),
    ];
    const labels = tripOptionLabels(trips);
    const values = trips.map((t) => labels.get(t.id));
    expect(new Set(values).size).toBe(4);
    expect(values[0]).toBe(
      "ORD → JFK · 2026-11-17 – 2026-11-19 · Draft · created 2026-11-01",
    );
    expect(values[1]).toBe(
      "ORD → JFK · 2026-11-17 – 2026-11-19 · Draft · created 2026-11-02 (1)",
    );
    expect(values[3]).toBe(
      "ORD → JFK · 2026-11-17 – 2026-11-19 · Draft · created 2026-11-02 (3)",
    );
  });
});

const SIGNED: ExpenseEntry = {
  id: "e1",
  organization_id: "o1",
  report_id: "r1",
  trip_segment_id: null,
  category: "meals",
  amount: "60.00",
  currency: "USD",
  expense_date: "2026-11-10",
  vendor: "Corner Cafe",
  description: null,
  status: "flagged",
  match_source: "manual",
  is_itemized: false,
  justification: "Lost it",
  receipt_affidavit_at: "2026-11-11T09:00:00Z",
  created_by: null,
  updated_by: null,
  created_at: "2026-11-10T00:00:00Z",
  updated_at: "2026-11-10T00:00:00Z",
  receipt_id: null,
  receipt_ids: [],
  receipts: [],
} as ExpenseEntry;

const MISSING: ReadinessFlag = {
  severity: "review",
  message: "Missing-receipt affidavit signed",
  clauseRef: "EXP-04",
  entryRef: "Corner Cafe",
  flagType: "missing_receipt",
  entryId: "e1",
  affidavitAllowed: true,
};

function editor() {
  return render(
    <EntryEditor
      entry={SIGNED}
      report={{ id: "r1", version: 3 }}
      flags={[MISSING]}
      onClose={() => {}}
    />,
  );
}

describe("EXP-04 affidavit follows the claim", () => {
  it.each([
    ["Amount (USD)", "65.00"],
    ["Date", "2026-11-09"],
    ["Vendor", "Other Cafe"],
    ["Description", "Team lunch"],
  ])("changing %s unticks it at once", (label, value) => {
    editor();
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
    expect(box.checked).toBe(false);
    expect(
      screen.getByTestId("entry-receipt-affidavit-cleared").textContent,
    ).toMatch(/tick it again/i);
  });

  it("can be re-signed for the edited claim", () => {
    editor();
    fireEvent.change(screen.getByLabelText("Amount (USD)"), {
      target: { value: "65.00" },
    });
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    fireEvent.click(box);
    expect(box.checked).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    const [vars] = h.updateMutate.mock.calls[0];
    // The server withdraws a signature when the claim changes, so re-ticking
    // must re-sign explicitly for the edited claim.
    expect(vars.changes).toEqual({ amount: "65.00", receipt_affidavit: true });
  });

  it("unchanged evidence keeps it ticked", () => {
    editor();
    fireEvent.change(screen.getByLabelText("Category"), {
      target: { value: "meals" },
    });
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
      true,
    );
  });
});

function report(over: Partial<ExpenseReport>): ExpenseReport {
  return {
    id: "r1",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "A-approve",
    status: "reimbursed",
    total_amount: "8.40",
    currency: "USD",
    submitted_at: "2026-11-14T00:00:00Z",
    pre_submit_flags: {
      flags: [
        {
          flag_type: "approval_required",
          severity: "advisory",
          reason: "Mileage needs manager confirmation",
          source_clause_ref: "MIL-02",
        },
      ],
    },
    created_by: null,
    updated_by: null,
    created_at: "2026-11-14T00:00:00Z",
    updated_at: "2026-11-14T00:00:00Z",
    version: 3,
    ...over,
  } as ExpenseReport;
}

function mountDetail(r: ExpenseReport) {
  h.useExpenseReport.mockReturnValue({
    isLoading: false,
    isError: false,
    data: r,
    refetch: vi.fn(),
  });
  h.useReportEntries.mockReturnValue({
    isLoading: false,
    isError: false,
    data: [],
    refetch: vi.fn(),
  });
  render(<ReportDetail reportId={r.id} onBack={() => {}} />);
}

describe("decided reports drop the pre-submit notice", () => {
  it.each(["reimbursed", "approved"] as const)(
    "a %s report shows no pre-submit check",
    (status) => {
      mountDetail(report({ status }));
      expect(screen.queryByText("Pre-submit check")).toBeNull();
      expect(
        screen.queryByText(/require approver sign-off after submission/),
      ).toBeNull();
    },
  );

  it("a draft still gets the pre-submit check", () => {
    mountDetail(report({ status: "draft" }));
    expect(screen.getByText("Pre-submit check")).toBeTruthy();
  });
});
