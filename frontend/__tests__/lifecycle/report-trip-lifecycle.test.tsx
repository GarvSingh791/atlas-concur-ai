/**
 * Product gaps 1 / 4 / 8 — report & trip lifecycle in the UI.
 *
 * - Withdraw is offered ONLY to the owner of a RETURNED report / trip (confirm
 *   dialog says it cannot be undone); a withdrawn report is read-only, badged
 *   "Withdrawn", and its lines can be selected to move out.
 * - "Mark reimbursed" is offered ONLY to a finance session on an APPROVED report
 *   it does not own; the confirm dialog states amount, title, owner and that no
 *   real money moves. The owner sees "Reimbursed" with date and reference.
 * - "Mark completed" is offered on an APPROVED trip whose end date has passed,
 *   to its traveler or finance — never to anyone else, never before it ends.
 * - The finance "Avg Cycle Time" with nothing reimbursed reads "—", not 0.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
} from "@testing-library/react";

const h = vi.hoisted(() => ({
  useExpenseReports: vi.fn(),
  useExpenseReport: vi.fn(),
  useReportEntries: vi.fn(),
  withdrawReport: vi.fn(),
  reimburse: vi.fn(),
  withdrawTrip: vi.fn(),
  completeTrip: vi.fn(),
  queue: vi.fn(),
  session: {
    user: { id: "emp" } as { id: string } | null,
    role: "employee" as string,
    perms: [] as string[],
  },
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useExpenseReports: h.useExpenseReports,
  useExpenseReport: h.useExpenseReport,
  useReportEntries: h.useReportEntries,
  useSubmitReport: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useMoveEntries: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/query/trip-hooks", () => ({
  useSubmitTrip: () => ({ mutate: vi.fn(), isPending: false }),
  useDiscardTrip: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/query/lifecycle-hooks", () => ({
  useWithdrawReport: () => ({ mutate: h.withdrawReport, isPending: false }),
  useReimburseReport: () => ({ mutate: h.reimburse, isPending: false }),
  useWithdrawTrip: () => ({ mutate: h.withdrawTrip, isPending: false }),
  useCompleteTrip: () => ({ mutate: h.completeTrip, isPending: false }),
  useReimbursementQueue: h.queue,
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({
    user: h.session.user,
    role: h.session.role,
    hasPermission: (p: string) => h.session.perms.includes(p),
  }),
}));

import { ReportDetail } from "@/components/expenses/report-detail";
import { ReportList } from "@/components/expenses/report-list";
import { TripApprovalPanel } from "@/components/trips/trip-approval-panel";
import { filterTrips } from "@/components/trips/trip-list";
import { ReimbursementQueueWidget } from "@/components/dashboard/widgets/reimbursement-queue-widget";
import { KpiWidget } from "@/components/dashboard/widgets/kpi-widget";
import {
  canCompleteTrip,
  canReimburse,
  canWithdrawReport,
  canWithdrawTrip,
  displayReportStatus,
  todayIsoUtc,
} from "@/lib/lifecycle/status";
import type { ExpenseReport, TripListItem } from "@/lib/types";

const TODAY = todayIsoUtc();
const daysFromToday = (n: number) => {
  const d = new Date(`${TODAY}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
};

function report(over: Partial<ExpenseReport> = {}): ExpenseReport {
  return {
    id: "r1",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "NYC trip",
    status: "draft",
    total_amount: "750.00",
    currency: "USD",
    submitted_at: "2026-06-12T00:00:00Z",
    pre_submit_flags: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 4,
    owner_name: "Erin Employee",
    ...over,
  };
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
  h.useExpenseReports.mockReturnValue({
    data: { items: [], total: 0, page: 1, size: 100 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  render(<ReportDetail reportId={r.id} onBack={() => {}} />);
}

function asEmployee(id = "emp") {
  h.session.user = { id };
  h.session.role = "employee";
  h.session.perms = ["expense.update.own"];
}
function asFinance(id = "fin") {
  h.session.user = { id };
  h.session.role = "finance_admin";
  h.session.perms = ["expense.reimburse.org", "expense.read.org"];
}

beforeEach(() => {
  vi.clearAllMocks();
  asEmployee();
});
afterEach(cleanup);

// ---------------------------------------------------------------------------
// Pure predicates
// ---------------------------------------------------------------------------

describe("lifecycle predicates", () => {
  it("withdraw: only a returned (changes requested / rejected) draft", () => {
    expect(
      canWithdrawReport({
        status: "draft",
        approval_status: "changes_requested",
      }),
    ).toBe(true);
    expect(
      canWithdrawReport({ status: "draft", approval_status: "rejected" }),
    ).toBe(true);
    for (const r of [
      { status: "draft" as const, approval_status: null },
      { status: "submitted" as const, approval_status: "pending" },
      { status: "approved" as const, approval_status: "approved" },
      { status: "reimbursed" as const, approval_status: "approved" },
      {
        status: "draft" as const,
        approval_status: "changes_requested",
        withdrawn_at: "2026-06-20T00:00:00Z",
      },
    ]) {
      expect(canWithdrawReport(r)).toBe(false);
    }
  });

  it("badge status: a withdrawn draft reads withdrawn", () => {
    expect(
      displayReportStatus({ status: "draft", withdrawn_at: "2026-06-20" }),
    ).toBe("withdrawn");
    expect(displayReportStatus({ status: "draft" })).toBe("draft");
  });

  it("reimburse: finance, approved, and never the viewer's own report", () => {
    const r = { status: "approved" as const, owner_id: "emp" };
    expect(canReimburse(r, { id: "fin" }, true)).toBe(true);
    expect(canReimburse(r, { id: "emp" }, true)).toBe(false); // SoD
    expect(canReimburse(r, { id: "fin" }, false)).toBe(false); // no permission
    expect(
      canReimburse({ ...r, status: "reimbursed" }, { id: "fin" }, true),
    ).toBe(false);
    expect(canReimburse(r, null, true)).toBe(false);
  });

  it("complete: approved and ended (end date <= today, UTC)", () => {
    expect(
      canCompleteTrip({ status: "approved", end_date: TODAY }, TODAY),
    ).toBe(true);
    expect(
      canCompleteTrip(
        { status: "approved", end_date: daysFromToday(1) },
        TODAY,
      ),
    ).toBe(false);
    expect(
      canCompleteTrip({ status: "draft", end_date: daysFromToday(-3) }, TODAY),
    ).toBe(false);
    expect(
      canWithdrawTrip({
        status: "draft",
        approval_status: "changes_requested",
      }),
    ).toBe(true);
    expect(canWithdrawTrip({ status: "draft", approval_status: null })).toBe(
      false,
    );
  });
});

// ---------------------------------------------------------------------------
// Report detail
// ---------------------------------------------------------------------------

describe("report detail — withdraw (gap 4)", () => {
  it("the owner of a returned report can withdraw it after confirming", () => {
    mountDetail(
      report({ status: "draft", approval_status: "changes_requested" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Withdraw report" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toMatch(/can’t be undone/);
    fireEvent.change(within(dialog).getByRole("textbox"), {
      target: { value: "Trip cancelled" },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Withdraw report" }),
    );
    expect(h.withdrawReport).toHaveBeenCalledTimes(1);
    expect(h.withdrawReport.mock.calls[0][0]).toEqual({
      id: "r1",
      version: 4,
      reason: "Trip cancelled",
    });
  });

  it("never offered to a non-owner, a never-submitted draft or a report under review", () => {
    asEmployee("mgr");
    mountDetail(
      report({ status: "draft", approval_status: "changes_requested" }),
    );
    expect(
      screen.queryByRole("button", { name: "Withdraw report" }),
    ).toBeNull();
    cleanup();
    asEmployee();
    mountDetail(report({ status: "draft", approval_status: null }));
    expect(
      screen.queryByRole("button", { name: "Withdraw report" }),
    ).toBeNull();
    cleanup();
    mountDetail(report({ status: "submitted", approval_status: "pending" }));
    expect(
      screen.queryByRole("button", { name: "Withdraw report" }),
    ).toBeNull();
  });

  it("a withdrawn report is read-only, badged Withdrawn, with its note", () => {
    mountDetail(
      report({
        status: "draft",
        approval_status: "changes_requested",
        withdrawn_at: "2026-06-20T10:00:00Z",
        withdrawal_reason: "Trip cancelled",
      }),
    );
    expect(screen.getByText("Withdrawn")).toBeTruthy();
    const note = screen.getByTestId("withdrawn-notice");
    expect(note.textContent).toContain("read-only");
    expect(note.textContent).toContain("Trip cancelled");
    expect(screen.queryByRole("button", { name: /submit report/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Rename report" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Withdraw report" }),
    ).toBeNull();
    expect(screen.queryByText(/This report is editable again/)).toBeNull();
  });
});

describe("report detail — reimbursement (gap 1)", () => {
  it("finance sees Mark reimbursed on someone else's approved report; the dialog states amount, title, owner and that no money moves", () => {
    asFinance();
    mountDetail(report({ status: "approved", approval_status: "approved" }));
    fireEvent.click(screen.getByRole("button", { name: "Mark reimbursed" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("$750.00");
    expect(dialog.textContent).toContain("NYC trip");
    expect(dialog.textContent).toContain("Erin Employee");
    expect(dialog.textContent).toMatch(/no real money moves/);
    fireEvent.click(
      within(dialog).getByRole("button", { name: /Mark \$750\.00 reimbursed/ }),
    );
    expect(h.reimburse).toHaveBeenCalledTimes(1);
    expect(h.reimburse.mock.calls[0][0]).toEqual({ id: "r1", version: 4 });
  });

  it("never offered on finance's own report, to a non-finance user, or before approval", () => {
    asFinance("emp"); // finance user owning the report
    mountDetail(report({ status: "approved", approval_status: "approved" }));
    expect(
      screen.queryByRole("button", { name: "Mark reimbursed" }),
    ).toBeNull();
    cleanup();
    asEmployee("mgr");
    mountDetail(report({ status: "approved", approval_status: "approved" }));
    expect(
      screen.queryByRole("button", { name: "Mark reimbursed" }),
    ).toBeNull();
    cleanup();
    asFinance();
    mountDetail(report({ status: "submitted", approval_status: "pending" }));
    expect(
      screen.queryByRole("button", { name: "Mark reimbursed" }),
    ).toBeNull();
  });

  it("the owner sees Reimbursed with date and reference", () => {
    mountDetail(
      report({
        status: "reimbursed",
        approval_status: "approved",
        reimbursed_at: "2026-06-25T09:00:00Z",
        reimbursement_reference: "REIMB-20260625-ABCDEF123456",
        reimbursement_amount: "750.00",
      }),
    );
    expect(screen.getByText("Reimbursed")).toBeTruthy();
    const note = screen.getByTestId("reimbursed-notice");
    expect(note.textContent).toContain("Jun 25, 2026");
    expect(note.textContent).toContain("REIMB-20260625-ABCDEF123456");
    expect(note.textContent).toMatch(/no real money moved/);
  });
});

describe("report list", () => {
  it("the Withdrawn filter asks the server for withdrawn reports only", () => {
    h.useExpenseReports.mockReturnValue({
      data: { items: [], total: 0, page: 1, size: 20 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<ReportList onSelectReport={() => {}} />);
    expect(h.useExpenseReports.mock.calls.at(-1)?.[0]?.withdrawn).toBe(
      undefined,
    );
    fireEvent.keyDown(screen.getByRole("button", { name: /All statuses/ }), {
      key: "Enter",
    });
    fireEvent.click(screen.getByRole("menuitem", { name: "Withdrawn" }));
    const params = h.useExpenseReports.mock.calls.at(-1)?.[0];
    expect(params.withdrawn).toBe(true);
    expect(params.status).toBeUndefined();
  });

  it("a reimbursed row shows Reimbursed with the paid date and the reference", () => {
    h.useExpenseReports.mockReturnValue({
      data: {
        items: [
          report({
            status: "reimbursed",
            approval_status: "approved",
            reimbursed_at: "2026-06-25T09:00:00Z",
            reimbursement_reference: "REIMB-20260625-ABCDEF123456",
            reimbursement_amount: "750.00",
          }),
        ],
        total: 1,
        page: 1,
        size: 20,
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<ReportList onSelectReport={() => {}} />);
    expect(screen.getAllByText("Reimbursed").length).toBeGreaterThan(0);
    const paid = screen.getByTestId("report-reimbursed-r1");
    expect(paid.textContent).toContain("Jun 25, 2026");
    expect(paid.textContent).toContain("REIMB-20260625-ABCDEF123456");
  });
});

// ---------------------------------------------------------------------------
// Trips
// ---------------------------------------------------------------------------

function tripItem(over: Partial<TripListItem> = {}): TripListItem {
  return {
    id: "t1",
    organization_id: "org",
    traveler_id: "emp",
    origin: "SFO",
    primary_destination: "JFK",
    start_date: daysFromToday(-5),
    end_date: daysFromToday(-2),
    currency: "USD",
    status: "approved",
    version: 2,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    approval_status: "approved",
    approval_reason: null,
    ...over,
  };
}

describe("trip panel — withdraw / mark completed (gaps 4 / 8)", () => {
  it("the traveler can mark an ended approved trip completed", () => {
    render(<TripApprovalPanel trip={tripItem()} title="SFO → JFK" />);
    fireEvent.click(screen.getByRole("button", { name: "Mark completed" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "Mark completed",
      }),
    );
    expect(h.completeTrip.mock.calls[0][0]).toEqual({ id: "t1" });
  });

  it("finance may complete it too; a manager reader may not; never before it ends", () => {
    asFinance();
    render(<TripApprovalPanel trip={tripItem()} />);
    expect(screen.getByRole("button", { name: "Mark completed" })).toBeTruthy();
    cleanup();
    h.session.user = { id: "mgr" };
    h.session.role = "manager";
    render(<TripApprovalPanel trip={tripItem()} />);
    expect(screen.queryByRole("button", { name: "Mark completed" })).toBeNull();
    cleanup();
    asEmployee();
    render(
      <TripApprovalPanel trip={tripItem({ end_date: daysFromToday(2) })} />,
    );
    expect(screen.queryByRole("button", { name: "Mark completed" })).toBeNull();
  });

  it("the traveler of a returned trip can withdraw it (confirm says it can't be undone)", () => {
    render(
      <TripApprovalPanel
        trip={tripItem({
          status: "draft",
          approval_status: "changes_requested",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Withdraw" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toMatch(/can’t be undone/);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Withdraw trip" }),
    );
    expect(h.withdrawTrip.mock.calls[0][0]).toEqual({
      id: "t1",
      reason: undefined,
    });
  });

  it("withdraw is never offered to a non-traveler or on a non-returned trip; a withdrawn trip reads Withdrawn", () => {
    h.session.user = { id: "mgr" };
    render(
      <TripApprovalPanel
        trip={tripItem({
          status: "draft",
          approval_status: "changes_requested",
        })}
      />,
    );
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
    cleanup();
    asEmployee();
    render(
      <TripApprovalPanel
        trip={tripItem({ status: "draft", approval_status: null })}
      />,
    );
    expect(screen.queryByRole("button", { name: "Withdraw" })).toBeNull();
    cleanup();
    render(
      <TripApprovalPanel
        trip={tripItem({
          status: "cancelled",
          approval_status: "changes_requested",
          withdrawn_at: "2026-06-20T00:00:00Z",
        })}
      />,
    );
    expect(screen.getByText("Withdrawn")).toBeTruthy();
  });

  it("trip filters separate completed, cancelled and withdrawn", () => {
    const trips = [
      tripItem({ id: "a", status: "completed" }),
      tripItem({ id: "b", status: "cancelled" }),
      tripItem({ id: "c", status: "cancelled", withdrawn_at: "2026-06-20" }),
      tripItem({ id: "d", status: "approved" }),
    ];
    expect(filterTrips(trips, "completed").map((t) => t.id)).toEqual(["a"]);
    expect(filterTrips(trips, "cancelled").map((t) => t.id)).toEqual(["b"]);
    expect(filterTrips(trips, "withdrawn").map((t) => t.id)).toEqual(["c"]);
    expect(filterTrips(trips, "all")).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// Finance dashboard
// ---------------------------------------------------------------------------

describe("Ready to reimburse panel (gap 1)", () => {
  const items = [
    {
      id: "r1",
      title: "NYC trip",
      owner_id: "emp",
      owner_name: "Erin Employee",
      total_amount: "750.00",
      currency: "USD",
      submitted_at: "2026-06-12T00:00:00Z",
      approval_status: "approved",
      version: 4,
      can_reimburse: true,
    },
    {
      id: "r2",
      title: "My own claim",
      owner_id: "fin",
      owner_name: "Fran Finance",
      total_amount: "30.00",
      currency: "USD",
      submitted_at: "2026-06-13T00:00:00Z",
      approval_status: "approved",
      version: 1,
      can_reimburse: false,
    },
  ];

  it("lists approved reports; the action is never offered on finance's own report", () => {
    asFinance();
    h.queue.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items, total: 2 },
      refetch: vi.fn(),
    });
    render(<ReimbursementQueueWidget />);
    const rows = screen.getAllByTestId("reimbursement-row");
    expect(rows).toHaveLength(2);
    expect(
      within(rows[0]).getByRole("button", { name: "Mark NYC trip reimbursed" }),
    ).toBeTruthy();
    expect(within(rows[1]).queryByRole("button")).toBeNull();
    fireEvent.click(
      within(rows[0]).getByRole("button", { name: "Mark NYC trip reimbursed" }),
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Erin Employee");
    expect(dialog.textContent).toMatch(/no real money moves/);
  });

  it("renders nothing for a session without the reimburse permission", () => {
    asEmployee();
    h.queue.mockReturnValue({
      isLoading: false,
      isError: false,
      data: { items, total: 2 },
      refetch: vi.fn(),
    });
    const { container } = render(<ReimbursementQueueWidget />);
    expect(container.textContent).toBe("");
    expect(h.queue).toHaveBeenCalledWith(false);
  });
});

describe("finance KPIs", () => {
  it("an average with nothing to average reads — (not 0) and carries its definition", () => {
    render(
      <KpiWidget
        kpis={[
          {
            label: "Avg Cycle Time",
            value: null,
            unit: "days",
            trend: null,
            trend_direction: null,
            description:
              "Average days from submission to (simulated) reimbursement.",
          },
          {
            label: "Avg Time to Decision",
            value: 1.5,
            unit: "days",
            trend: null,
            trend_direction: null,
          },
        ]}
        isLoading={false}
        isError={false}
      />,
    );
    const tiles = screen.getAllByTestId("kpi-tile");
    expect(tiles[0].textContent).toContain("—");
    expect(tiles[0].textContent).not.toContain("0.0 days");
    expect(tiles[0].getAttribute("title")).toMatch(/reimbursement/);
    expect(tiles[1].textContent).toContain("1.5 days");
  });
});
