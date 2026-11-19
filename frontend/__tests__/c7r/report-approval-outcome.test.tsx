/**
 * C7R issue 4b (P12 contract) — reports carry their owner-visible
 * `approval_status` + `approval_reason`. A request-changes decision returns
 * the report to `draft` (editable); a reject moves it to `rejected`. The
 * owner's views must say so and show the approver's comment verbatim.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

const { useExpenseReports, useExpenseReport, useReportEntries, session } =
  vi.hoisted(() => ({
    useExpenseReports: vi.fn(),
    useExpenseReport: vi.fn(),
    useReportEntries: vi.fn(),
    // UAT2: owner copy is keyed on the session user owning the report.
    session: { user: { id: "emp" } as { id: string } | null },
  }));
vi.mock("@/lib/query/expense-hooks", () => ({
  useExpenseReports,
  useExpenseReport,
  useReportEntries,
  useSubmitReport: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ ...session, hasPermission: () => true }),
}));

import { ReportDetail } from "@/components/expenses/report-detail";
import { ReportList } from "@/components/expenses/report-list";
import { RejectionNotice } from "@/components/expenses/rejection-notice";
import { normalizePreSubmitFlags } from "@/lib/expenses/readiness";
import type { ExpenseReport, ReportListItem } from "@/lib/types";

function report(over: Partial<ExpenseReport>): ExpenseReport {
  return {
    id: "r1",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "NYC trip",
    status: "draft",
    total_amount: "120.00",
    currency: "USD",
    submitted_at: "2026-06-12T00:00:00Z",
    pre_submit_flags: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 3,
    ...over,
  };
}

function mountDetail(r: ExpenseReport) {
  useExpenseReport.mockReturnValue({
    isLoading: false,
    isError: false,
    data: r,
    refetch: vi.fn(),
  });
  useReportEntries.mockReturnValue({
    isLoading: false,
    isError: false,
    data: [],
    refetch: vi.fn(),
  });
  render(<ReportDetail reportId={r.id} onBack={() => {}} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  session.user = { id: "emp" };
});
afterEach(cleanup);

describe("C7R report approval outcome (owner view)", () => {
  it("returned for changes: draft + notice with the approver's comment", () => {
    mountDetail(
      report({
        status: "draft",
        approval_status: "changes_requested",
        approval_reason: "Attach the hotel folio, please.",
      }),
    );
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Attach the hotel folio, please.")).toBeTruthy();
    expect(screen.getByText("Draft")).toBeTruthy();
    expect(screen.queryByText(/doesn’t expose approval decision notes/)).toBe(
      null,
    );
    // The owner is told it is theirs to fix — and can submit it again.
    expect(screen.getByText(/This report is editable again/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /submit report/i })).toBeTruthy();
    expect(screen.queryByRole("note", { name: "Read-only report" })).toBeNull();
  });

  it("rejected: shows the reviewer's comment instead of the gap note", () => {
    mountDetail(
      report({
        status: "rejected",
        approval_status: "rejected",
        approval_reason: "Personal expense — not reimbursable.",
      }),
    );
    expect(screen.getByText("Report rejected")).toBeTruthy();
    expect(
      screen.getByText("Personal expense — not reimbursable."),
    ).toBeTruthy();
    expect(screen.queryByText(/doesn’t expose approval decision notes/)).toBe(
      null,
    );
  });

  it("approved after a decision: badge only, no notices", () => {
    mountDetail(report({ status: "approved", approval_status: "approved" }));
    expect(screen.getByText("Approved")).toBeTruthy();
    expect(screen.queryByText("Report rejected")).toBeNull();
    expect(screen.queryByText("Changes requested")).toBeNull();
  });

  it("older backend (no approval fields): rejected keeps the honest gap note", () => {
    render(
      <RejectionNotice readiness={normalizePreSubmitFlags(null)} ownerView />,
    );
    expect(
      screen.getByText(/doesn’t expose approval decision notes/),
    ).toBeTruthy();
  });

  it("report list flags a returned draft for the owner", () => {
    const items: ReportListItem[] = [
      {
        ...report({ id: "r-ret", title: "Returned one" }),
        approval_status: "changes_requested",
      },
      { ...report({ id: "r-plain", title: "Plain draft" }) },
    ].map(
      ({ pre_submit_flags: _p, created_by: _c, updated_by: _u, ...r }) => r,
    );
    useExpenseReports.mockReturnValue({
      data: { items, total: 2, page: 1, size: 20 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<ReportList onSelectReport={() => {}} />);
    expect(
      screen.getAllByText(/Changes requested — edit and resubmit/),
    ).toHaveLength(1);
  });

  it("a non-draft report never shows the 'editable again' notice", () => {
    // approval_status can lag (e.g. a resubmitted report whose new approval
    // has not been created yet) — only a DRAFT is actually editable.
    mountDetail(
      report({
        status: "submitted",
        approval_status: "changes_requested",
        approval_reason: "Attach the hotel folio, please.",
      }),
    );
    expect(screen.queryByText("Changes requested")).toBeNull();
    expect(screen.queryByText(/This report is editable again/)).toBeNull();
  });
});

// UAT2 Issue 7 — a manager opening a teammate's report sees it read-only.
describe("report approval outcome (non-owner view)", () => {
  beforeEach(() => {
    session.user = { id: "mgr" };
  });

  it("returned for changes: comment shown, no owner copy, no submit, read-only banner", () => {
    mountDetail(
      report({
        status: "draft",
        approval_status: "changes_requested",
        approval_reason: "Attach the hotel folio, please.",
        owner_name: "Erin Employee",
      }),
    );
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Attach the hotel folio, please.")).toBeTruthy();
    expect(screen.queryByText(/This report is editable again/)).toBeNull();
    expect(screen.queryByText(/your approver/)).toBeNull();
    expect(screen.queryByRole("button", { name: /submit report/i })).toBeNull();
    const note = screen.getByRole("note", { name: "Read-only report" });
    expect(note.textContent).toContain("Viewing Erin Employee’s report");
    expect(note.textContent).toContain("read-only");
  });

  it("rejected: never says it can be re-submitted", () => {
    mountDetail(
      report({
        status: "rejected",
        approval_status: "rejected",
        approval_reason: "Personal expense — not reimbursable.",
        pre_submit_flags: {
          warnings: [{ severity: "high", message: "Over the cap" }],
        },
      }),
    );
    expect(screen.getByText("Report rejected")).toBeTruthy();
    expect(screen.queryByText(/\bre-?submit/i)).toBeNull();
    // No owner name from an older backend → generic, still read-only.
    expect(
      screen.getByRole("note", { name: "Read-only report" }).textContent,
    ).toContain("Viewing another employee’s report");
  });

  it("no banner while the session is still hydrating (and no owner actions)", () => {
    session.user = null;
    mountDetail(report({ status: "draft" }));
    expect(screen.queryByRole("note", { name: "Read-only report" })).toBeNull();
    expect(screen.queryByRole("button", { name: /submit report/i })).toBeNull();
  });
});
