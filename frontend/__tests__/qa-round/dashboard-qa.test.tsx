/**
 * QA round — dashboards.
 *
 *  - Defect 3: at 390px the employee dashboard clipped the status badges in
 *    Trip Timeline / Recent Expenses ("Appr", "Reimbu"). jsdom has no layout, so
 *    this pins the layout contract that prevents it: the widget grid's single
 *    column may shrink below its content (grid-cols-1 + min-w-0), the text
 *    column takes the slack and truncates, and badges never shrink or wrap.
 *  - Defect 5: Trip Timeline shows each trip status accurately (completed is
 *    "Completed", never "Approved").
 *  - Defect 8: manager dashboard shows a human subject type, currency with 2
 *    decimals and readable activity text with no raw action keys or UUIDs.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup, within } from "@testing-library/react";

vi.mock("@/lib/query/analytics-hooks", () => ({
  useAnalyticsOverview: vi.fn(),
  useTrips: vi.fn(),
  useExpenseReports: vi.fn(),
}));
vi.mock("next/navigation", () => ({
  useRouter: vi.fn(() => ({ push: vi.fn() })),
}));

import {
  useAnalyticsOverview,
  useTrips,
  useExpenseReports,
} from "@/lib/query/analytics-hooks";
import { TimelineWidget } from "@/components/dashboard/widgets/timeline-widget";
import { QueueWidget } from "@/components/dashboard/widgets/queue-widget";
import { EmployeeDashboard } from "@/components/dashboard/employee-dashboard";
import { ManagerDashboard } from "@/components/dashboard/manager-dashboard";
import { activityText, subjectTypeLabel } from "@/lib/dashboard/format";

const mockOverview = useAnalyticsOverview as ReturnType<typeof vi.fn>;
const mockTrips = useTrips as ReturnType<typeof vi.fn>;
const mockReports = useExpenseReports as ReturnType<typeof vi.fn>;

afterEach(cleanup);

const UUID = "4f1c2a9e-0b7d-4c41-9a51-3f0e8d2b6c11";

function trip(id: string, status: string) {
  return {
    id,
    origin: "SFO",
    primary_destination: "JFK",
    start_date: "2026-06-10",
    end_date: "2026-06-12",
    status,
    currency: "USD",
  };
}

describe("defect 5 — trip timeline status labels", () => {
  it("labels every trip status accurately", () => {
    render(
      <TimelineWidget
        data={{
          items: [
            trip("t1", "completed"),
            trip("t2", "approved"),
            trip("t3", "pending_approval"),
            trip("t4", "cancelled"),
            trip("t5", "draft"),
            trip("t6", "rejected"),
          ],
          total: 6,
        }}
        isLoading={false}
        isError={false}
      />,
    );
    const label = (id: string) =>
      screen.getByTestId(`timeline-status-${id}`).textContent;
    expect(label("t1")).toBe("Completed");
    expect(label("t2")).toBe("Approved");
    expect(label("t3")).toBe("Pending approval");
    expect(label("t4")).toBe("Cancelled");
    expect(label("t5")).toBe("Draft");
    expect(label("t6")).toBe("Rejected");
  });
});

describe("defect 3 — employee dashboard at phone width", () => {
  it("lets the widget column shrink, truncates text and keeps badges whole", () => {
    mockOverview.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockTrips.mockReturnValue({
      data: { items: [trip("t1", "approved")], total: 1 },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    mockReports.mockReturnValue({
      data: {
        items: [
          {
            id: "r1",
            title: "A very long expense report title that needs truncation",
            status: "reimbursed",
            total_amount: "1117.50",
            currency: "USD",
            created_at: "2026-11-14T00:00:00Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<EmployeeDashboard />);

    const grid = screen.getByTestId("employee-widgets");
    expect(grid.className).toContain("grid-cols-1");
    for (const child of Array.from(grid.children)) {
      expect((child as HTMLElement).className).toContain("min-w-0");
    }

    const tripBadge = screen.getByTestId("timeline-status-t1");
    expect(tripBadge.className).toContain("shrink-0");
    expect(tripBadge.className).toContain("whitespace-nowrap");
    const tripText = tripBadge.previousElementSibling as HTMLElement;
    expect(tripText.className).toContain("min-w-0");
    expect(tripText.className).toContain("flex-1");

    const reportRow = screen.getByTestId("recent-expense-r1");
    const reportBadge = within(reportRow).getByText("Reimbursed");
    expect(reportBadge.className).toContain("whitespace-nowrap");
    expect(reportBadge.className).toContain("shrink-0");
    const title = within(reportRow).getByText(/A very long expense report/);
    expect(title.className).toContain("truncate");
    expect((title.parentElement as HTMLElement).className).toContain("flex-1");
  });
});

describe("defect 8 — manager dashboard formatting", () => {
  it("formats subject type and money in the approval queue", () => {
    render(
      <QueueWidget
        items={[
          {
            id: "a1",
            subject_type: "expense_report",
            submitter_name: "Alex Employee",
            amount: 1117.5,
            currency: "USD",
            age_days: 2,
            status: "pending",
          },
          {
            id: "a2",
            subject_type: "trip",
            submitter_name: "Alex Employee",
            amount: 750,
            currency: "USD",
            age_days: 0,
            status: "pending",
          },
        ]}
        total={2}
        isLoading={false}
        isError={false}
      />,
    );
    expect(screen.getByText("$1,117.50")).toBeTruthy();
    expect(screen.getByText("$750.00")).toBeTruthy();
    expect(screen.queryByText(/expense_report/)).toBeNull();
    expect(screen.getByText(/Expense report · 2d ago/)).toBeTruthy();
    expect(screen.getByText(/Trip · today/)).toBeTruthy();
  });

  it("humanizes action keys and subject types", () => {
    expect(activityText("expense_report.withdrawn")).toBe(
      "Expense report withdrawn",
    );
    expect(activityText("approval.decided")).toBe("Approval decided");
    expect(activityText("expense_report.status_changed")).toBe(
      "Expense report status changed",
    );
    expect(activityText("weird")).toBe("Weird");
    expect(subjectTypeLabel("expense_report")).toBe("Expense report");
    expect(subjectTypeLabel("trip")).toBe("Trip");
    expect(subjectTypeLabel("policy_rule")).toBe("Policy rule");
  });

  it("recent activity shows readable text and never a UUID", () => {
    mockOverview.mockReturnValue({
      data: {
        manager: {
          approval_queue: [],
          approval_queue_total: 0,
          team_spend: { by_category: [] },
          recent_activity: [
            {
              id: "l1",
              action: "expense_report.withdrawn",
              actor_name: "Alex Employee",
              entity_type: "expense_report",
              occurred_at: "2026-11-14T20:00:00Z",
              summary: `expense_report.withdrawn on expense_report ${UUID}`,
            },
          ],
        },
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    const { container } = render(<ManagerDashboard />);
    expect(screen.getByText("Expense report withdrawn")).toBeTruthy();
    expect(container.textContent).not.toContain(UUID);
    expect(container.textContent).not.toContain("expense_report");
    expect(screen.getByText(/Alex Employee · Expense report ·/)).toBeTruthy();
  });
});
