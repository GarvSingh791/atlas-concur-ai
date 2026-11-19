/**
 * Product gap 5 — Team list + member drill-down (server truth, read-only).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get, role } = vi.hoisted(() => ({
  get: vi.fn(),
  role: { value: "manager" },
}));
vi.mock("@/lib/api/client", () => ({ api: { get } }));
// QA round: the member drill-down keeps its tab / opened report in the URL.
vi.mock("next/navigation", () => ({
  usePathname: () => "/team/m-1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ user: { id: "mgr" }, role: role.value }),
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
  TripApprovalPanel: ({
    trip,
    title,
    children,
  }: {
    trip: { traveler_name?: string };
    title: string;
    children: React.ReactNode;
  }) => (
    <div data-testid="trip-panel">
      {title} — {trip.traveler_name}
      {children}
    </div>
  ),
}));
vi.mock("@/components/trips/trip-segment-list", () => ({
  TripSegmentList: () => <div>SEGMENTS</div>,
}));

import { TeamScreen } from "@/components/team/team-screen";
import { TeamMemberScreen } from "@/components/team/team-member-screen";
import { ApiError } from "@/lib/types/api";
import type { TeamMemberSummary } from "@/lib/types/team";

function member(over: Partial<TeamMemberSummary>): TeamMemberSummary {
  return {
    id: "m-1",
    name: "Alice Report",
    department: "Engineering",
    role: "employee",
    open_trips: 2,
    reports_by_status: {
      draft: 1,
      submitted: 3,
      approved: 0,
      rejected: 0,
      reimbursed: 0,
    },
    pending_approvals: 1,
    spend_mtd: "100.50",
    spend_90d: "300.50",
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

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  get.mockReset();
  role.value = "manager";
});
afterEach(cleanup);

describe("TeamScreen", () => {
  it("lists members with links, counts and Decimal spend", async () => {
    get.mockResolvedValue(
      page([
        member({}),
        member({ id: "m-2", name: "Bob", other_currency_reports: 1 }),
      ]),
    );
    wrap(<TeamScreen />);
    const links = await screen.findAllByTestId("team-member-link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "/team/m-1",
      "/team/m-2",
    ]);
    expect(screen.getByText("$300.50")).toBeTruthy();
    expect(screen.getByText("$300.50 *")).toBeTruthy(); // Bob: foreign-currency note
    expect(screen.getByText("2 people")).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/team/members", {
      params: { page: 1, size: 20, sort: "name" },
    });
  });

  it("sorts and searches on the server", async () => {
    get.mockResolvedValue(page([member({})]));
    wrap(<TeamScreen />);
    await screen.findAllByTestId("team-member-link");
    fireEvent.change(screen.getByLabelText("Sort by"), {
      target: { value: "-spend_90d" },
    });
    await waitFor(() =>
      expect(get).toHaveBeenLastCalledWith("/team/members", {
        params: { page: 1, size: 20, sort: "-spend_90d" },
      }),
    );
    fireEvent.change(
      screen.getByPlaceholderText("Search by name or department"),
      {
        target: { value: "  eng " },
      },
    );
    await waitFor(() =>
      expect(get).toHaveBeenLastCalledWith("/team/members", {
        params: { page: 1, size: 20, q: "eng", sort: "-spend_90d" },
      }),
    );
  });

  it("explains an empty team", async () => {
    get.mockResolvedValue(page([]));
    wrap(<TeamScreen />);
    expect(await screen.findByText("No direct reports yet")).toBeTruthy();
  });
});

describe("TeamMemberScreen", () => {
  const detail = {
    member: member({}),
    trips: page([
      {
        id: "t-1",
        organization_id: "o",
        traveler_id: "m-1",
        origin: "NYC",
        primary_destination: "SFO",
        start_date: "2026-05-01",
        end_date: "2026-05-03",
        currency: "USD",
        status: "draft",
        version: 1,
        created_at: "",
        updated_at: "",
      },
    ]),
    reports: page([
      {
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
      },
    ]),
  };

  it("shows KPIs and read-only trips, then reports in the other tab", async () => {
    get.mockResolvedValue(detail);
    wrap(<TeamMemberScreen memberId="m-1" />);
    expect(
      await screen.findByRole("heading", { name: "Alice Report" }),
    ).toBeTruthy();
    expect(get).toHaveBeenCalledWith("/team/members/m-1", {
      params: { trips_page: 1, reports_page: 1, size: 10 },
    });
    // Trip panel gets the member's name for its read-only banner.
    expect(screen.getByTestId("trip-panel").textContent).toContain(
      "Alice Report",
    );
    fireEvent.click(screen.getByRole("tab", { name: "Reports (1)" }));
    fireEvent.click(await screen.findByTestId("team-report-row"));
    expect(screen.getByText("REPORT DETAIL r-1")).toBeTruthy();
    fireEvent.click(screen.getByText("back"));
    expect(
      screen
        .getByRole("tab", { name: "Reports (1)" })
        .getAttribute("aria-selected"),
    ).toBe("true");
  });

  it("a member outside the team reads as not found", async () => {
    get.mockRejectedValue(
      new ApiError(
        { code: "NOT_FOUND", message: "x", correlation_id: "c" },
        404,
      ),
    );
    wrap(<TeamMemberScreen memberId="someone" />);
    expect(await screen.findByText("Not on your team")).toBeTruthy();
  });

  it("empty tabs have empty states", async () => {
    get.mockResolvedValue({ ...detail, trips: page([]), reports: page([]) });
    wrap(<TeamMemberScreen memberId="m-1" />);
    expect(await screen.findByText("No trips")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Reports (0)" }));
    expect(screen.getByText("No expense reports")).toBeTruthy();
  });
});
