import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { DecisionPanel } from "@/components/approvals/decision-panel";
import type { ExpenseEntry, ExpenseReport } from "@/lib/types";
import type {
  ApprovalDetail,
  ApprovalTripSegment,
  ApprovalTripSummary,
} from "@/lib/types/approvals";

afterEach(cleanup);

function entry(over: Partial<ExpenseEntry>): ExpenseEntry {
  return {
    id: "e1",
    organization_id: "org",
    report_id: "r1",
    trip_segment_id: null,
    category: "lodging",
    amount: "867.00",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Hyatt",
    description: "Hotel — 3 nights",
    status: "flagged",
    match_source: "manual",
    is_itemized: false,
    created_by: "u1",
    updated_by: "u1",
    created_at: "2026-06-10T00:00:00Z",
    updated_at: "2026-06-10T00:00:00Z",
    ...over,
  };
}

function report(): ExpenseReport {
  return {
    id: "r1",
    organization_id: "org",
    owner_id: "u1",
    trip_id: null,
    title: "Q3 Conference & Travel",
    status: "submitted",
    total_amount: "1234.50",
    currency: "USD",
    submitted_at: "2026-06-12T00:00:00Z",
    pre_submit_flags: null,
    created_by: "u1",
    updated_by: "u1",
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 1,
  };
}

const EXPENSE_DETAIL: ApprovalDetail = {
  id: "ap-1",
  organization_id: "org",
  subject_type: "expense_report",
  subject_id: "r1",
  submitter_id: "u1",
  submitter_name: "Alex Employee",
  current_status: "pending",
  is_auto_approved: false,
  risk_summary: {
    route: "escalate",
    subject_type: "expense_report",
    subject_in_policy: false,
    blocking_flag_count: 1,
    requires_approver: false,
    subject_total_usd: 1234.5,
    flags: [
      {
        entry_ref: "Hotel — 3 nights",
        source_clause_ref: "HOTEL-CAP",
        reason: "Nightly rate exceeds cap.",
        severity: "blocking",
      },
    ],
  },
  risk_level: "high",
  version: 1,
  created_at: "2026-06-12T00:00:00Z",
  updated_at: "2026-06-12T00:00:00Z",
  steps: [],
  expense_report: { report: report(), entries: [entry({})] },
  trip: null,
};

function tripSummary(): ApprovalTripSummary {
  return {
    id: "t1",
    organization_id: "org",
    traveler_id: "u1",
    origin: "LAX",
    primary_destination: "ORD",
    start_date: "2026-07-15",
    end_date: "2026-07-17",
    purpose: "Client",
    estimated_cost: "1410.00",
    budget_amount: "1800.00",
    currency: "USD",
    status: "pending_approval",
    recommendation_context: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    version: 1,
  };
}

function segment(over: Partial<ApprovalTripSegment>): ApprovalTripSegment {
  return {
    id: "s1",
    trip_id: "t1",
    segment_type: "hotel",
    supplier: "Hyatt Regency",
    start_datetime: "2026-07-15T16:00:00Z",
    end_datetime: "2026-07-17T11:00:00Z",
    location: "Chicago, IL",
    amount: "890.00",
    currency: "USD",
    in_policy: false,
    policy_notes: "Nightly rate over cap.",
    details: null,
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

const TRIP_DETAIL: ApprovalDetail = {
  ...EXPENSE_DETAIL,
  id: "ap-2",
  subject_type: "trip",
  subject_id: "t1",
  risk_level: "medium",
  risk_summary: {
    route: "escalate",
    subject_type: "trip",
    subject_in_policy: false,
    blocking_flag_count: 0,
    requires_approver: false,
    subject_total_usd: 1410,
    flags: [
      {
        segment_id: "s1",
        segment_type: "hotel",
        in_policy: false,
        policy_notes: "Over cap",
      },
    ],
  },
  expense_report: null,
  trip: { trip: tripSummary(), segments: [segment({})] },
};

function baseProps() {
  return {
    isLoading: false,
    isError: false,
    onRetry: vi.fn(),
    onBack: vi.fn(),
    canDecide: true,
  };
}

// The panel renders DecisionActions, which uses TanStack Query hooks.
function renderPanel(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>,
  );
}

describe("DecisionPanel", () => {
  it("prompts to select when nothing is chosen", () => {
    renderPanel(
      <DecisionPanel approvalId={null} detail={null} {...baseProps()} />,
    );
    expect(
      screen.getByText("Select an approval to review its details."),
    ).toBeTruthy();
  });

  it("renders an expense_report subject: AI analysis, citation, entries (G10)", () => {
    renderPanel(
      <DecisionPanel
        approvalId="ap-1"
        detail={EXPENSE_DETAIL}
        {...baseProps()}
      />,
    );
    expect(screen.getByText("Q3 Conference & Travel")).toBeTruthy();
    expect(screen.getByText("Escalated for human review")).toBeTruthy();
    // exception reason + policy citation chip
    expect(screen.getByText("Nightly rate exceeds cap.")).toBeTruthy();
    expect(screen.getByText("HOTEL-CAP")).toBeTruthy();
    // entries table (subject context) — assert via the vendor, unique to the table
    expect(screen.getByText("Hyatt")).toBeTruthy();
    expect(screen.getByText("High risk")).toBeTruthy();
  });

  it("renders a trip subject: segments, not an entries table (G10)", () => {
    renderPanel(
      <DecisionPanel approvalId="ap-2" detail={TRIP_DETAIL} {...baseProps()} />,
    );
    expect(screen.getByText("LAX → ORD")).toBeTruthy();
    expect(screen.getByText("Nightly rate over cap.")).toBeTruthy();
    // trip exceptions carry no clause-ref citation
    expect(screen.queryByText("HOTEL-CAP")).toBeNull();
  });

  it("renders an error state with retry", () => {
    const onRetry = vi.fn();
    renderPanel(
      <DecisionPanel
        approvalId="ap-1"
        detail={null}
        {...baseProps()}
        isError
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText("Couldn’t load this approval")).toBeTruthy();
  });

  it("shows decision history when steps are present", () => {
    const withStep: ApprovalDetail = {
      ...EXPENSE_DETAIL,
      current_status: "rejected",
      steps: [
        {
          id: "st1",
          approver_id: "m1",
          approver_name: "Morgan Manager",
          step_order: 1,
          decision: "reject",
          reason: "Lodging exceeds policy",
          decided_at: "2026-06-13T00:00:00Z",
          created_at: "2026-06-13T00:00:00Z",
        },
      ],
    };
    renderPanel(
      <DecisionPanel approvalId="ap-1" detail={withStep} {...baseProps()} />,
    );
    expect(screen.getByText(/Morgan Manager/)).toBeTruthy();
    expect(screen.getByText("“Lodging exceeds policy”")).toBeTruthy();
  });
});
