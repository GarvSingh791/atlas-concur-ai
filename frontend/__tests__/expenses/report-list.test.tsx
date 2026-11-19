import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import { ReportList } from "@/components/expenses/report-list";
import type { ReportListItem } from "@/lib/types";

// Mock the data hook so the list renders deterministic, owner-scoped rows.
const { useExpenseReports } = vi.hoisted(() => ({
  useExpenseReports: vi.fn(),
}));
vi.mock("@/lib/query/expense-hooks", () => ({ useExpenseReports }));

afterEach(cleanup);

function row(overrides: Partial<ReportListItem>): ReportListItem {
  return {
    id: "r",
    organization_id: "org-1",
    owner_id: "emp-1",
    trip_id: null,
    title: "Report",
    status: "draft",
    total_amount: "0.00",
    currency: "USD",
    submitted_at: null,
    version: 1,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    ...overrides,
  };
}

const ITEMS: ReportListItem[] = [
  row({
    id: "r-rej",
    title: "Conference Expenses (Rejected)",
    status: "rejected",
    total_amount: "545.00",
    submitted_at: "2026-04-10T00:00:00Z",
  }),
  row({
    id: "r-draft",
    title: "June Miscellaneous",
    status: "draft",
    total_amount: "73.00",
  }),
  row({
    id: "r-app",
    title: "Q2 Summit Expenses",
    status: "approved",
    total_amount: "1351.00",
    submitted_at: "2026-05-15T00:00:00Z",
  }),
];

beforeEach(() => {
  useExpenseReports.mockReturnValue({
    data: { items: ITEMS, total: 3, page: 1, size: 20, pages: 1 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
});

describe("ReportList", () => {
  it("renders a lifecycle status badge per report", () => {
    render(<ReportList onSelectReport={() => {}} />);
    expect(screen.getByText("Rejected")).toBeTruthy();
    expect(screen.getByText("Draft")).toBeTruthy();
    expect(screen.getByText("Approved")).toBeTruthy();
  });

  it("emphasizes a rejected report as needing review", () => {
    render(<ReportList onSelectReport={() => {}} />);
    expect(screen.getByText("Needs your review")).toBeTruthy();
  });

  it("formats the amount straight from total_amount", () => {
    render(<ReportList onSelectReport={() => {}} />);
    expect(screen.getByText("$545.00")).toBeTruthy();
    expect(screen.getByText("$1,351.00")).toBeTruthy();
  });

  it("requests the default sort + pagination params on mount", () => {
    render(<ReportList onSelectReport={() => {}} />);
    const params = useExpenseReports.mock.calls[0][0];
    expect(params.page).toBe(1);
    expect(params.size).toBe(20);
    expect(params.sort).toBe("submitted_at:desc");
    expect(params.status).toBeUndefined();
  });

  it("filters rows client-side by the search term (title)", () => {
    render(<ReportList onSelectReport={() => {}} />);
    const input = screen.getByPlaceholderText("Search reports");
    fireEvent.change(input, { target: { value: "june" } });
    expect(screen.getByText("June Miscellaneous")).toBeTruthy();
    expect(screen.queryByText("Conference Expenses (Rejected)")).toBeNull();
  });

  it("opens a report when its View action is clicked", () => {
    const onSelect = vi.fn();
    render(<ReportList onSelectReport={onSelect} />);
    fireEvent.click(screen.getByLabelText("Open June Miscellaneous"));
    expect(onSelect).toHaveBeenCalledWith("r-draft");
  });
});
