/**
 * C7R UAT — a returned report can be fixed and reports can be assembled.
 *
 *  (A) Report detail, DRAFT + OWNER: each entry has a real "Edit" action; the
 *      inline editor PATCHes only the changed fields (justification included)
 *      with the report version as If-Match; flags offer "Add justification";
 *      a justified flag is shown as such and no longer blocks submit. A stale
 *      version surfaces as "This report changed". Non-draft / non-owner views
 *      stay read-only. The lineage badge is a label, not a fake control.
 *  (B) "New report" replaces the disabled "Assemble report" + dev note; entries
 *      can be selected and moved to another draft (or a new one); a draft can
 *      be renamed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
  act,
} from "@testing-library/react";

const h = vi.hoisted(() => ({
  useExpenseReports: vi.fn(),
  useExpenseReport: vi.fn(),
  useReportEntries: vi.fn(),
  updateMutate: vi.fn(),
  createMutate: vi.fn(),
  renameMutate: vi.fn(),
  moveMutate: vi.fn(),
  showToast: vi.fn(),
  session: {
    user: { id: "emp" } as { id: string } | null,
    hasPermission: (() => true) as (key: string) => boolean,
  },
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useExpenseReports: h.useExpenseReports,
  useExpenseReport: h.useExpenseReport,
  useReportEntries: h.useReportEntries,
  useSubmitReport: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateEntry: () => ({ mutate: h.updateMutate, isPending: false }),
  useCreateReport: () => ({ mutate: h.createMutate, isPending: false }),
  useRenameReport: () => ({ mutate: h.renameMutate, isPending: false }),
  useMoveEntries: () => ({ mutate: h.moveMutate, isPending: false }),
  // UAT2 — delete / attach (behaviour covered in uat2-expense-lifecycle.test.tsx).
  useDeleteEntry: () => ({ mutate: vi.fn(), isPending: false }),
  useDeleteReport: () => ({ mutate: vi.fn(), isPending: false }),
  useLinkReceipt: () => ({ mutate: vi.fn(), isPending: false }),
  forgetDeletedReport: vi.fn(),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({ mutate: vi.fn(), isPending: false }),
  useAttachReceipt: () => ({ mutate: vi.fn(), isPending: false }),
  useAttachableReceipts: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/lib/query/trip-hooks", () => ({
  useTrips: () => ({
    data: {
      items: [
        {
          id: "trip-mine",
          traveler_id: "emp",
          origin: "SFO",
          primary_destination: "ORD",
          start_date: "2026-06-09",
          currency: "USD",
        },
        {
          id: "trip-other",
          traveler_id: "someone-else",
          origin: "JFK",
          primary_destination: "LAX",
          start_date: "2026-06-01",
          currency: "USD",
        },
      ],
    },
  }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => h.session,
}));

import { ReportDetail } from "@/components/expenses/report-detail";
import { ReportList } from "@/components/expenses/report-list";
import { ApiError } from "@/lib/types/api";
import {
  normalizePreSubmitFlags,
  suggestedFix,
} from "@/lib/expenses/readiness";
import { computeSubmitGate } from "@/lib/expenses/submit-gate";
import type { ExpenseEntry, ExpenseReport, ReportListItem } from "@/lib/types";

const CAP_REASON =
  "Hotel amount 600.00 USD exceeds the domestic cap 180.00 USD per night. Requires manager pre-approval.";

function report(over: Partial<ExpenseReport> = {}): ExpenseReport {
  return {
    id: "rep-1",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "Receipt Inbox",
    status: "draft",
    total_amount: "612.00",
    currency: "USD",
    submitted_at: null,
    pre_submit_flags: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 7,
    ...over,
  };
}

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-hotel",
    organization_id: "org",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "lodging",
    amount: "600.00",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Grand Hotel",
    description: null,
    status: "flagged",
    match_source: null,
    is_itemized: false,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    receipt_id: null,
    justification: null,
    ...over,
  };
}

function capFlags(justification: string | null = null) {
  return {
    flags: [
      {
        entry_ref: "Grand Hotel",
        entry_id: "e-hotel",
        flag_type: "policy_violation",
        rule_type: "amount_cap",
        source_clause_ref: "HOTEL-CAP",
        reason: CAP_REASON,
        severity: justification ? "advisory" : "blocking",
        justifiable: true,
        justification,
      },
    ],
    blocking_count: justification ? 0 : 1,
    advisory_count: justification ? 1 : 0,
  };
}

const ENTRIES = [
  entry(),
  entry({
    id: "e-coffee",
    category: "meals",
    amount: "12.00",
    vendor: "Cafe",
    status: "draft",
  }),
];

function mount(r: ExpenseReport, entries: ExpenseEntry[] = ENTRIES) {
  h.useExpenseReport.mockReturnValue({
    isLoading: false,
    isError: false,
    data: r,
    refetch: vi.fn(),
  });
  h.useReportEntries.mockReturnValue({
    isLoading: false,
    isError: false,
    data: entries,
    refetch: vi.fn(),
  });
  return render(<ReportDetail reportId={r.id} onBack={() => {}} />);
}

function listItem(over: Partial<ReportListItem>): ReportListItem {
  return {
    id: "r",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "Report",
    status: "draft",
    total_amount: "0.00",
    currency: "USD",
    submitted_at: null,
    version: 1,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...over,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  h.session.user = { id: "emp" };
  h.session.hasPermission = () => true;
  h.useExpenseReports.mockReturnValue({
    data: {
      items: [
        listItem({ id: "rep-1", title: "Receipt Inbox" }),
        listItem({ id: "rep-chi", title: "Chicago conference", version: 2 }),
        listItem({ id: "rep-mgr", title: "Not mine", owner_id: "mgr" }),
      ],
      total: 3,
      page: 1,
      size: 100,
      pages: 1,
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
});
afterEach(cleanup);

// ---------------------------------------------------------------------------
// (A) edit + justification
// ---------------------------------------------------------------------------

describe("report detail — editable draft (owner)", () => {
  it("edits an entry inline and PATCHes only the changed fields with If-Match", () => {
    mount(report({ pre_submit_flags: capFlags() }));
    fireEvent.click(screen.getByRole("button", { name: "Edit Grand Hotel" }));

    const form = screen.getByRole("form", { name: "Edit expense entry" });
    fireEvent.change(within(form).getByLabelText("Amount (USD)"), {
      target: { value: "175.00" },
    });
    fireEvent.change(within(form).getByLabelText("Justification"), {
      target: { value: "  Sold-out conference week. " },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: /save changes/i }),
    );

    expect(h.updateMutate).toHaveBeenCalledTimes(1);
    const [args, callbacks] = h.updateMutate.mock.calls[0];
    expect(args).toEqual({
      entryId: "e-hotel",
      reportId: "rep-1",
      reportVersion: 7,
      changes: { amount: "175.00", justification: "Sold-out conference week." },
    });

    act(() => callbacks.onSuccess());
    expect(h.showToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success", title: "Entry updated" }),
    );
    expect(
      screen.queryByRole("form", { name: "Edit expense entry" }),
    ).toBeNull();
  });

  it("rejects an invalid amount client-side (no request)", () => {
    mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Edit Cafe" }));
    const form = screen.getByRole("form", { name: "Edit expense entry" });
    fireEvent.change(within(form).getByLabelText("Amount (USD)"), {
      target: { value: "12.345" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: /save changes/i }),
    );
    expect(h.updateMutate).not.toHaveBeenCalled();
    expect(within(form).getByRole("alert").textContent).toMatch(/amount/i);
  });

  it("a stale version (409) reads as 'This report changed'", () => {
    mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Edit Cafe" }));
    const form = screen.getByRole("form", { name: "Edit expense entry" });
    fireEvent.change(within(form).getByLabelText("Vendor"), {
      target: { value: "Cafe Roma" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: /save changes/i }),
    );
    const [, callbacks] = h.updateMutate.mock.calls[0];
    act(() => {
      callbacks.onError(
        new ApiError(
          { code: "CONFLICT", message: "stale", correlation_id: "c" },
          409,
        ),
      );
    });
    expect(h.showToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: "error",
        title: "This report changed",
      }),
    );
    // The stale form closes so reopening starts from the refetched version.
    expect(
      screen.queryByRole("form", { name: "Edit expense entry" }),
    ).toBeNull();
  });

  it("keeps the version the form was opened at when the detail refetches mid-edit", () => {
    const view = mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Edit Cafe" }));
    // Another tab edits the report: the detail refetches at version 8 with a
    // new vendor while this form is still open.
    h.useExpenseReport.mockReturnValue({
      isLoading: false,
      isError: false,
      data: report({ version: 8 }),
      refetch: vi.fn(),
    });
    h.useReportEntries.mockReturnValue({
      isLoading: false,
      isError: false,
      data: ENTRIES.map((e) =>
        e.vendor === "Cafe" ? { ...e, description: "Edited elsewhere" } : e,
      ),
      refetch: vi.fn(),
    });
    view.rerender(<ReportDetail reportId="rep-1" onBack={() => {}} />);

    const form = screen.getByRole("form", { name: "Edit expense entry" });
    fireEvent.change(within(form).getByLabelText("Vendor"), {
      target: { value: "Cafe Roma" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: /save changes/i }),
    );
    const [args] = h.updateMutate.mock.calls[0];
    // If-Match is the version the user started from → the server 409s rather
    // than the save silently overwriting the concurrent change.
    expect(args.reportVersion).toBe(7);
    expect(args.changes).toEqual({ vendor: "Cafe Roma" });
  });

  it("a justifiable flag offers 'Add justification' and focuses the box", () => {
    mount(report({ pre_submit_flags: capFlags() }));
    expect(
      screen.getByText(
        "Adjust the amount to within policy, or add a justification.",
      ),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add justification" }));
    const box = screen.getByLabelText("Justification");
    expect(document.activeElement).toBe(box);
    // The editor lists the server's flag message for this entry.
    const form = screen.getByRole("form", { name: "Edit expense entry" });
    expect(within(form).getByText(CAP_REASON)).toBeTruthy();
  });

  it("a justified flag is shown to the user as such and no longer blocks submit", () => {
    const justified = capFlags("Sold-out conference week.");
    mount(report({ pre_submit_flags: justified }), [
      entry({ status: "draft", justification: "Sold-out conference week." }),
    ]);
    expect(screen.getByText(/Justification added/)).toBeTruthy();
    expect(
      screen.getAllByText("Sold-out conference week.").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "Edit justification" }),
    ).toBeTruthy();
    // Policy column: advisory, not "In policy".
    expect(screen.getByText("Advisory")).toBeTruthy();
    const readiness = normalizePreSubmitFlags(justified);
    expect(readiness.level).toBe("review");
    expect(computeSubmitGate(readiness.level).canSubmit).toBe(true);
  });

  it("selects entries and moves them to another of the owner's drafts", () => {
    mount(report());
    fireEvent.click(screen.getByLabelText("Select row e-hotel"));
    const bar = screen.getByRole("region", { name: "Move selected entries" });
    expect(within(bar).getByText("1 entry selected")).toBeTruthy();

    const select = within(bar).getByLabelText("Move to report");
    const options = within(select as HTMLElement)
      .getAllByRole("option")
      .map((o) => o.textContent);
    // Own other drafts only — not this report, not someone else's.
    expect(options).toEqual(["Move to…", "Chicago conference", "New report…"]);

    fireEvent.change(select, { target: { value: "rep-chi" } });
    fireEvent.click(within(bar).getByRole("button", { name: /move/i }));
    expect(h.moveMutate).toHaveBeenCalledWith(
      {
        sourceId: "rep-1",
        sourceVersion: 7,
        targetId: "rep-chi",
        entryIds: ["e-hotel"],
      },
      expect.anything(),
    );
  });

  it("'New report…' creates a draft, then moves the selection into it", () => {
    mount(report());
    fireEvent.click(screen.getByLabelText("Select row e-coffee"));
    const bar = screen.getByRole("region", { name: "Move selected entries" });
    fireEvent.change(within(bar).getByLabelText("Move to report"), {
      target: { value: "__new__" },
    });
    fireEvent.click(within(bar).getByRole("button", { name: /move/i }));

    fireEvent.change(screen.getByLabelText("Report title"), {
      target: { value: "Chicago meals" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create report" }));
    const [body, callbacks] = h.createMutate.mock.calls[0];
    expect(body).toEqual({
      title: "Chicago meals",
      currency: "USD",
      trip_id: null,
    });
    act(() =>
      callbacks.onSuccess(report({ id: "rep-new", title: "Chicago meals" })),
    );
    expect(h.moveMutate).toHaveBeenCalledWith(
      expect.objectContaining({ targetId: "rep-new", entryIds: ["e-coffee"] }),
      expect.anything(),
    );
  });

  it("renames the draft with the report version", () => {
    mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Rename report" }));
    const input = screen.getByLabelText("Report title");
    fireEvent.change(input, { target: { value: "Chicago conference" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(h.renameMutate).toHaveBeenCalledWith(
      { id: "rep-1", version: 7, title: "Chicago conference" },
      expect.anything(),
    );
  });
});

// ---------------------------------------------------------------------------
// Segregation of duties — a returned report keeps its entries (C7R review)
// ---------------------------------------------------------------------------

describe("report detail — Move on a returned report", () => {
  it.each(["changes_requested", "rejected"])(
    "%s: Move is disabled with the reason, and nothing is sent",
    (approvalStatus) => {
      mount(report({ approval_status: approvalStatus }));
      // Entries can still be selected and edited — the fix happens here.
      fireEvent.click(screen.getByLabelText("Select row e-hotel"));
      expect(
        screen.getByRole("button", { name: "Edit Grand Hotel" }),
      ).toBeTruthy();
      const bar = screen.getByRole("region", { name: "Move selected entries" });

      const select = within(bar).getByLabelText(
        "Move to report",
      ) as HTMLSelectElement;
      expect(select.disabled).toBe(true);
      const moveButton = within(bar)
        .getAllByRole("button", { name: /move/i })
        .find((b) => b.hasAttribute("disabled")) as HTMLButtonElement;
      expect(moveButton).toBeTruthy();
      expect(moveButton.disabled).toBe(true);

      const reason = within(bar).getByText(/A reviewer returned this report/);
      expect(reason.id).toBe("move-locked-reason");
      expect(moveButton.getAttribute("aria-describedby")).toBe(
        "move-locked-reason",
      );
      fireEvent.click(moveButton);
      expect(h.moveMutate).not.toHaveBeenCalled();
    },
  );

  it("offers a returned draft as a move target, labelled as returned", () => {
    h.useExpenseReports.mockReturnValue({
      data: {
        items: [
          listItem({ id: "rep-1", title: "Receipt Inbox" }),
          listItem({ id: "rep-chi", title: "Chicago conference" }),
          listItem({
            id: "rep-back",
            title: "Sent back",
            approval_status: "changes_requested",
          }),
        ],
        total: 3,
        page: 1,
        size: 100,
        pages: 1,
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mount(report());
    fireEvent.click(screen.getByLabelText("Select row e-hotel"));
    const bar = screen.getByRole("region", { name: "Move selected entries" });
    const options = within(within(bar).getByLabelText("Move to report"))
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toEqual([
      "Move to…",
      "Chicago conference",
      "Sent back (returned for changes)",
      "New report…",
    ]);
  });

  it("a fresh draft (no approval) can still move entries", () => {
    mount(report({ approval_status: null }));
    fireEvent.click(screen.getByLabelText("Select row e-hotel"));
    const bar = screen.getByRole("region", { name: "Move selected entries" });
    expect(
      (within(bar).getByLabelText("Move to report") as HTMLSelectElement)
        .disabled,
    ).toBe(false);
    expect(
      within(bar).queryByText(/A reviewer returned this report/),
    ).toBeNull();
  });
});

describe("report detail — read-only cases", () => {
  it.each([
    ["submitted report", report({ status: "submitted" }), { id: "emp" }],
    ["someone else's draft", report(), { id: "mgr" }],
  ])(
    "%s: no edit, rename, selection or justification actions",
    (_l, r, user) => {
      h.session.user = user;
      mount(r as ExpenseReport);
      expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Rename report" }),
      ).toBeNull();
      expect(screen.queryByLabelText("Select row e-hotel")).toBeNull();
      expect(
        screen.queryByRole("button", { name: "Add justification" }),
      ).toBeNull();
    },
  );

  // UAT2 Issue 7 — every owner action keys on ownership, not just on status.
  const DELETABLE = ENTRIES.map((e) => ({ ...e, deletable: true }));

  it("owner's draft (control): submit, delete and attach controls are offered", () => {
    h.session.user = { id: "emp" };
    mount(report({ pre_submit_flags: capFlags() }), DELETABLE);
    expect(
      screen.getAllByRole("button", { name: /submit report/i }).length,
    ).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Delete report" })).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Attach receipt to Grand Hotel" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Delete Grand Hotel" }),
    ).toBeTruthy();
    expect(screen.queryByRole("note", { name: "Read-only report" })).toBeNull();
  });

  it("someone else's draft: no submit / delete / attach / move, read-only banner", () => {
    h.session.user = { id: "mgr" };
    mount(
      report({
        pre_submit_flags: capFlags(),
        approval_status: "changes_requested",
        approval_reason: "Use the negotiated rate.",
        owner_name: "Erin Employee",
      }),
      DELETABLE,
    );
    expect(screen.queryByRole("button", { name: /submit report/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /submit anyway/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete report" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /^Attach receipt/ }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /^Delete / })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Move/ })).toBeNull();
    expect(screen.queryByText(/This report is editable again/)).toBeNull();
    expect(screen.queryByText(/then resubmit/)).toBeNull();
    // The approver's comment and the issues stay visible.
    expect(screen.getByText("Use the negotiated rate.")).toBeTruthy();
    expect(
      screen.getByRole("note", { name: "Read-only report" }).textContent,
    ).toMatch(/Viewing Erin Employee’s report\s*— read-only/);
  });

  it("lineage shows a static provenance label (no 'Unmatched · Unmatched') and the justification", () => {
    h.session.user = { id: "mgr" };
    mount(report(), [entry({ justification: "Client dinner overflow." })]);
    fireEvent.click(screen.getByLabelText("Expand row"));
    expect(screen.getByText("Unmatched — no automatic source")).toBeTruthy();
    expect(screen.queryByText(/Unmatched · Unmatched/)).toBeNull();
    expect(screen.getByText("Client dinner overflow.")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// (B) report list — create
// ---------------------------------------------------------------------------

describe("report list — New report", () => {
  it("replaces the disabled Assemble button and opens the new report", () => {
    const onSelect = vi.fn();
    render(<ReportList onSelectReport={onSelect} />);
    expect(screen.queryByText(/Assemble report/)).toBeNull();
    expect(screen.queryByText(/P5-T10/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /new report/i }));
    fireEvent.change(screen.getByLabelText("Report title"), {
      target: { value: "  Chicago conference " },
    });
    // Only the viewer's own trips are offered.
    const trip = screen.getByLabelText("Trip (optional)");
    const tripOptions = within(trip as HTMLElement)
      .getAllByRole("option")
      .map((o) => o.textContent);
    // QA round: options carry the trip title (route · dates), see trip-options.
    expect(tripOptions).toEqual(["No trip", "SFO → ORD · 2026-06-09"]);
    fireEvent.change(trip, { target: { value: "trip-mine" } });
    fireEvent.click(screen.getByRole("button", { name: "Create report" }));

    const [body, callbacks] = h.createMutate.mock.calls[0];
    expect(body).toEqual({
      title: "Chicago conference",
      currency: "USD",
      trip_id: "trip-mine",
    });
    act(() =>
      callbacks.onSuccess(
        report({ id: "rep-new", title: "Chicago conference" }),
      ),
    );
    expect(onSelect).toHaveBeenCalledWith("rep-new");
  });

  it("a blank title is rejected without a request", () => {
    render(<ReportList onSelectReport={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /new report/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create report" }));
    expect(h.createMutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toMatch(/title/i);
  });

  it("is hidden for a viewer who cannot create reports (finance)", () => {
    h.session.hasPermission = () => false;
    render(<ReportList onSelectReport={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /new report/i })).toBeNull();
  });
});

describe("readiness normalizer — justification fields", () => {
  it("maps entry_id / justifiable / justification from the server flag", () => {
    const r = normalizePreSubmitFlags(capFlags("Because."));
    expect(r.flags[0]).toMatchObject({
      entryId: "e-hotel",
      ruleType: "amount_cap",
      justifiable: true,
      justification: "Because.",
      severity: "review",
    });
  });

  it("a non-justifiable violation does not suggest a justification", () => {
    const r = normalizePreSubmitFlags({
      flags: [
        {
          flag_type: "policy_violation",
          rule_type: "category_allow",
          severity: "blocking",
          justifiable: false,
          reason: "Luxury vehicles are not reimbursable.",
        },
      ],
    });
    expect(suggestedFix(r.flags[0])).not.toMatch(/justification/);
    // Older snapshots without the key keep the original wording.
    const legacy = normalizePreSubmitFlags({
      flags: [{ flag_type: "policy_violation", severity: "blocking" }],
    });
    expect(legacy.flags[0].justifiable).toBeUndefined();
    expect(suggestedFix(legacy.flags[0])).toMatch(/add a justification/);
  });
});
