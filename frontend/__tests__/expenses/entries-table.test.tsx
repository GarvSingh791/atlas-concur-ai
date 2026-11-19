import { describe, it, expect, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import { EntriesTable } from "@/components/expenses/entries-table";
import type { ExpenseEntry } from "@/lib/types";

afterEach(cleanup);

function entry(overrides: Partial<ExpenseEntry>): ExpenseEntry {
  return {
    id: "e-1",
    organization_id: "org-1",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "meals",
    amount: "42.50",
    currency: "USD",
    expense_date: "2026-06-11",
    vendor: "Vendor",
    description: null,
    status: "draft",
    match_source: null,
    is_itemized: false,
    created_by: "u-1",
    updated_by: "u-1",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    ...overrides,
  };
}

const ENTRIES: ExpenseEntry[] = [
  entry({
    id: "e-auto",
    category: "airfare",
    vendor: "United Airlines",
    amount: "320.00",
    match_source: "receipt",
    status: "matched",
    trip_segment_id: "seg-abcd1234-0000",
  }),
  entry({
    id: "e-manual",
    category: "meals",
    vendor: "Corner Cafe",
    amount: "45.00",
    match_source: "manual",
    status: "draft",
  }),
  entry({
    id: "e-flagged",
    category: "lodging",
    vendor: "Hilton",
    amount: "450.00",
    match_source: null,
    status: "flagged",
  }),
];

describe("EntriesTable", () => {
  it("maps each entry's match_source to a label (auto vs manual vs unmatched)", () => {
    render(<EntriesTable entries={ENTRIES} />);
    expect(screen.getByText("Auto · Receipt")).toBeTruthy();
    expect(screen.getByText("Manual")).toBeTruthy();
    expect(screen.getByText("Unmatched")).toBeTruthy();
  });

  it("derives the per-entry policy column from the server entry status", () => {
    render(<EntriesTable entries={ENTRIES} />);
    // Only the flagged entry is out of policy; the other two are in policy.
    expect(screen.getByText("Out of policy")).toBeTruthy();
    expect(screen.getAllByText("In policy").length).toBe(2);
  });

  it("renders money straight from the entry amount (no client total math)", () => {
    render(<EntriesTable entries={ENTRIES} />);
    expect(screen.getByText("$320.00")).toBeTruthy();
    expect(screen.getByText("$450.00")).toBeTruthy();
  });

  it("expands an auto-matched row to its lineage from the DTO", () => {
    render(<EntriesTable entries={ENTRIES} />);
    const expanders = screen.getAllByLabelText("Expand row");
    fireEvent.click(expanders[0]); // e-auto
    // C7R UAT: one phrase, no duplicated "Auto · " (was "Auto-matched · Auto · Receipt").
    expect(screen.getByText("Auto-matched · Receipt")).toBeTruthy();
    // No receipt_id on the DTO → a metadata note only (no image fetch, so the
    // table renders without a query client).
    expect(screen.getByText(/Linked from an uploaded receipt/i)).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("expands an unmatched row to an honest 'manually entered' note", () => {
    render(<EntriesTable entries={ENTRIES} />);
    const expanders = screen.getAllByLabelText("Expand row");
    fireEvent.click(expanders[2]); // e-flagged (match_source null)
    expect(screen.getByText(/Manually entered/i)).toBeTruthy();
  });
});
