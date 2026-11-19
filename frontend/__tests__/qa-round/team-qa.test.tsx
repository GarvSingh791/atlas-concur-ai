/**
 * QA round, defect 8 — team screens.
 *
 *  - A deactivated report on /team (and on their drill-down) is marked
 *    "Inactive".
 *  - The member drill-down's Reports tab and an opened report live in the URL
 *    (?tab=reports&report=<id>) so they are linkable and the browser Back
 *    button returns from a report to the member's report list.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get, push, replace, nav } = vi.hoisted(() => ({
  get: vi.fn(),
  push: vi.fn(),
  replace: vi.fn(),
  nav: { search: "" },
}));
vi.mock("@/lib/api/client", () => ({ api: { get } }));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ user: { id: "mgr" }, role: "manager" }),
}));
vi.mock("next/navigation", () => ({
  usePathname: () => "/team/m-1",
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));
vi.mock("@/components/expenses/report-detail", () => ({
  ReportDetail: ({
    reportId,
    onBack,
  }: {
    reportId: string;
    onBack: () => void;
  }) => (
    <div>
      REPORT DETAIL {reportId}
      <button onClick={onBack}>back</button>
    </div>
  ),
}));
vi.mock("@/components/trips/trip-approval-panel", () => ({
  TripApprovalPanel: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="trip-panel">{children}</div>
  ),
}));
vi.mock("@/components/trips/trip-segment-list", () => ({
  TripSegmentList: () => <div>SEGMENTS</div>,
}));

import { TeamScreen } from "@/components/team/team-screen";
import { TeamMemberScreen } from "@/components/team/team-member-screen";
import type { TeamMemberSummary } from "@/lib/types/team";

function member(over: Partial<TeamMemberSummary>): TeamMemberSummary {
  return {
    id: "m-1",
    name: "Alice Report",
    department: "Engineering",
    role: "employee",
    status: "active",
    open_trips: 0,
    reports_by_status: {
      draft: 0,
      submitted: 0,
      approved: 0,
      rejected: 0,
      reimbursed: 0,
    },
    pending_approvals: 0,
    spend_mtd: "0.00",
    spend_90d: "0.00",
    currency: "USD",
    other_currency_reports: 0,
    ...over,
  };
}

const page = <T,>(items: T[]) => ({
  items,
  total: items.length,
  page: 1,
  size: 20,
  pages: 1,
});

const REPORT = {
  id: "r-1",
  organization_id: "o",
  owner_id: "m-1",
  trip_id: null,
  title: "May trip",
  status: "submitted",
  total_amount: "75.00",
  currency: "USD",
  submitted_at: "2026-05-04T10:00:00Z",
  version: 1,
  created_at: "",
  updated_at: "",
};

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  get.mockReset();
  push.mockReset();
  replace.mockReset();
  nav.search = "";
});
afterEach(cleanup);

describe("inactive members", () => {
  it("marks a deactivated member on the team list", async () => {
    get.mockResolvedValue(
      page([
        member({}),
        member({ id: "m-2", name: "Gone Person", status: "inactive" }),
      ]),
    );
    wrap(<TeamScreen />);
    await screen.findAllByTestId("team-member-link");
    const markers = screen.getAllByTestId("team-member-inactive");
    expect(markers).toHaveLength(1);
    expect(markers[0].textContent).toBe("Inactive");
    expect(markers[0].closest("tr")?.textContent).toContain("Gone Person");
  });

  it("marks a deactivated member on the drill-down header", async () => {
    get.mockResolvedValue({
      member: member({ status: "inactive" }),
      trips: page([]),
      reports: page([]),
    });
    wrap(<TeamMemberScreen memberId="m-1" />);
    await screen.findByRole("heading", { name: /Alice Report/ });
    expect(screen.getByTestId("team-member-inactive").textContent).toBe(
      "Inactive",
    );
  });
});

describe("linkable member tabs", () => {
  const detail = {
    member: member({}),
    trips: page([]),
    reports: page([REPORT]),
  };

  it("switching to Reports writes ?tab=reports", async () => {
    get.mockResolvedValue(detail);
    wrap(<TeamMemberScreen memberId="m-1" />);
    fireEvent.click(await screen.findByRole("tab", { name: "Reports (1)" }));
    expect(replace).toHaveBeenLastCalledWith("/team/m-1?tab=reports", {
      scroll: false,
    });
  });

  it("opening a report pushes a history entry so Back returns to the list", async () => {
    get.mockResolvedValue(detail);
    nav.search = "tab=reports";
    wrap(<TeamMemberScreen memberId="m-1" />);
    expect(
      (await screen.findByRole("tab", { name: "Reports (1)" })).getAttribute(
        "aria-selected",
      ),
    ).toBe("true");
    fireEvent.click(screen.getByTestId("team-report-row"));
    expect(push).toHaveBeenLastCalledWith("/team/m-1?tab=reports&report=r-1", {
      scroll: false,
    });
    expect(screen.getByText("REPORT DETAIL r-1")).toBeTruthy();
  });

  it("a linked report URL opens that report; its back returns to Reports", async () => {
    get.mockResolvedValue(detail);
    nav.search = "tab=reports&report=r-1";
    wrap(<TeamMemberScreen memberId="m-1" />);
    expect(await screen.findByText("REPORT DETAIL r-1")).toBeTruthy();
    fireEvent.click(screen.getByText("back"));
    expect(replace).toHaveBeenLastCalledWith("/team/m-1?tab=reports", {
      scroll: false,
    });
  });
});
