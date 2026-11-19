import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

// vi.mock calls are hoisted to the top of the file by vitest
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: vi.fn(),
}));

vi.mock("@/lib/query/analytics-hooks", () => ({
  useAnalyticsOverview: vi.fn(),
  useTrips: vi.fn(),
  useExpenseReports: vi.fn(),
}));

// next/navigation mock — EmployeeDashboard calls useRouter
vi.mock("next/navigation", () => ({
  useRouter: vi.fn(() => ({ push: vi.fn() })),
}));

import { useSession } from "@/lib/auth/session-provider";
import {
  useAnalyticsOverview,
  useTrips,
  useExpenseReports,
} from "@/lib/query/analytics-hooks";
import DashboardPage from "@/app/(app)/dashboard/page";

const mockUseSession = useSession as ReturnType<typeof vi.fn>;
const mockUseOverview = useAnalyticsOverview as ReturnType<typeof vi.fn>;
const mockUseTrips = useTrips as ReturnType<typeof vi.fn>;
const mockUseExpenseReports = useExpenseReports as ReturnType<typeof vi.fn>;

const loadingState = {
  data: undefined,
  isLoading: true,
  isError: false,
  refetch: vi.fn(),
};

beforeEach(() => {
  mockUseOverview.mockReturnValue(loadingState);
  mockUseTrips.mockReturnValue(loadingState);
  mockUseExpenseReports.mockReturnValue(loadingState);
});

afterEach(cleanup);

describe("DashboardPage role composition", () => {
  it("renders EmployeeDashboard for employee role", () => {
    mockUseSession.mockReturnValue({ role: "employee" });
    render(<DashboardPage />);
    // Employee dashboard should NOT contain manager-specific content
    expect(screen.queryByText("Pending Approvals")).toBeNull();
    // Employee dashboard should be rendered (h1 Dashboard appears)
    expect(screen.getByText("Dashboard")).toBeTruthy();
  });

  it("renders ManagerDashboard for manager role", () => {
    mockUseSession.mockReturnValue({ role: "manager" });
    render(<DashboardPage />);
    expect(screen.getByText("Dashboard")).toBeTruthy();
  });

  it("renders FinanceDashboard for finance_admin role", () => {
    mockUseSession.mockReturnValue({ role: "finance_admin" });
    render(<DashboardPage />);
    expect(screen.getByText("Dashboard")).toBeTruthy();
  });

  it("employee dashboard has CTA to planner", () => {
    mockUseSession.mockReturnValue({ role: "employee" });
    render(<DashboardPage />);
    expect(screen.getByText(/Plan your next trip/i)).toBeTruthy();
  });

  it("manager dashboard does not show employee-only widgets", () => {
    mockUseSession.mockReturnValue({ role: "manager" });
    render(<DashboardPage />);
    // Employee CTA should not be present in manager view
    expect(screen.queryByText(/Plan your next trip/i)).toBeNull();
  });

  it("finance dashboard does not show employee-only widgets", () => {
    mockUseSession.mockReturnValue({ role: "finance_admin" });
    render(<DashboardPage />);
    expect(screen.queryByText(/Plan your next trip/i)).toBeNull();
  });
});

describe("EmployeeDashboard recent expenses", () => {
  it("formats report dates instead of rendering raw ISO timestamps", () => {
    mockUseSession.mockReturnValue({ role: "employee" });
    mockUseExpenseReports.mockReturnValue({
      data: {
        items: [
          {
            id: "r1",
            title: "Q4 offsite",
            status: "draft",
            created_at: "2026-11-08T22:11:35.564315Z",
          },
        ],
        total: 1,
      },
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    });
    render(<DashboardPage />);
    expect(screen.getByText("Q4 offsite")).toBeTruthy();
    expect(screen.queryByText(/2026-11-08T22:11:35/)).toBeNull();
    // Same "Mon D, YYYY" format as the rest of the app (lib/expenses/format);
    // the day depends on the runner's timezone.
    expect(screen.getByText(/^Nov (8|9), 2026$/)).toBeTruthy();
  });
});
