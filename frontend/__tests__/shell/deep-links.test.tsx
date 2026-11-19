import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, render, screen } from "@testing-library/react";

/**
 * Product gap 3: notification / search links open the subject directly
 *   /expenses?report=<id>   → that report's detail (read-only for non-owners)
 *   /approvals?approval=<id>→ that approval preselected
 *   /trips?trip=<id>        → that trip expanded
 */

let params = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useSearchParams: () => params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  // Integration: the approvals screen also keeps product gap 6 filters in the URL.
  usePathname: () => "/approvals",
}));
vi.mock("@/lib/query/approval-filter-hooks", () => ({
  useExceptionTypes: () => ({ data: [], isLoading: false }),
  useApprovalDepartments: () => ({ data: [], isLoading: false }),
}));
vi.mock("@/components/approvals/approval-filter-bar", () => ({
  ApprovalFilterBar: () => null,
}));
vi.mock("@/components/expenses/report-list", () => ({
  ReportList: () => <div>REPORT LIST</div>,
}));
vi.mock("@/components/expenses/report-detail", () => ({
  ReportDetail: ({ reportId }: { reportId: string }) => (
    <div>DETAIL {reportId}</div>
  ),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ user: { id: "mgr" }, hasPermission: () => true }),
}));
const approvalHooks = vi.hoisted(() => ({
  useApprovals: vi.fn(() => ({ data: { items: [] }, isLoading: false })),
  useApproval: vi.fn(() => ({ data: null, isLoading: false })),
}));
vi.mock("@/lib/query/approval-hooks", () => approvalHooks);
vi.mock("@/components/approvals/approval-queue", () => ({
  ApprovalQueue: () => null,
}));
vi.mock("@/components/approvals/decision-panel", () => ({
  DecisionPanel: ({ approvalId }: { approvalId: string | null }) => (
    <div>PANEL {approvalId ?? "none"}</div>
  ),
}));
vi.mock("@/lib/query/trip-hooks", () => ({
  useTrips: () => ({
    data: {
      items: [
        {
          id: "t1",
          origin: "NYC",
          primary_destination: "Paris",
          start_date: "2026-05-01",
          end_date: "2026-05-03",
        },
        {
          id: "t2",
          origin: "NYC",
          primary_destination: "Rome",
          start_date: "2026-06-01",
          end_date: "2026-06-03",
        },
      ],
    },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }),
}));
vi.mock("@/components/trips/trip-approval-panel", () => ({
  TripApprovalPanel: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
}));
vi.mock("@/components/trips/trip-segment-list", () => ({
  TripSegmentList: ({ tripId }: { tripId: string }) => (
    <div>SEGMENTS {tripId}</div>
  ),
}));

import { ExpensesScreen } from "@/components/expenses/expenses-screen";
import { ApprovalsScreen } from "@/components/approvals/approvals-screen";
import { TripList } from "@/components/trips/trip-list";

beforeEach(() => {
  params = new URLSearchParams();
  approvalHooks.useApproval.mockClear();
});
afterEach(cleanup);

describe("deep links", () => {
  it("/expenses opens the list without a param", () => {
    render(<ExpensesScreen />);
    expect(screen.getByText("REPORT LIST")).toBeTruthy();
  });

  it("/expenses?report=<id> opens that report", () => {
    params = new URLSearchParams("report=r9");
    render(<ExpensesScreen />);
    expect(screen.getByText("DETAIL r9")).toBeTruthy();
  });

  it("/approvals?approval=<id> preselects that approval", () => {
    params = new URLSearchParams("approval=a9");
    render(<ApprovalsScreen />);
    expect(screen.getByText("PANEL a9")).toBeTruthy();
    expect(approvalHooks.useApproval).toHaveBeenCalledWith("a9");
  });

  it("/approvals?approval=<id> keeps the URL filters (product gap 6) too", () => {
    params = new URLSearchParams("approval=a9&current_status=pending");
    render(<ApprovalsScreen />);
    expect(screen.getByText("PANEL a9")).toBeTruthy();
    expect(approvalHooks.useApprovals).toHaveBeenCalledWith(
      expect.objectContaining({ current_status: "pending" }),
      // QA round: the list query is disabled for an inverted date range.
      expect.objectContaining({ enabled: true }),
    );
  });

  it("/approvals without a param selects nothing", () => {
    render(<ApprovalsScreen />);
    expect(screen.getByText("PANEL none")).toBeTruthy();
  });

  it("/trips?trip=<id> expands only that trip", () => {
    params = new URLSearchParams("trip=t2");
    render(<TripList />);
    expect(screen.getByText("SEGMENTS t2")).toBeTruthy();
    expect(screen.queryByText("SEGMENTS t1")).toBeNull();
  });
});
