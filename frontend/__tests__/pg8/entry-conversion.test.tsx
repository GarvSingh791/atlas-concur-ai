import { afterEach, describe, expect, it } from "vitest";
import * as React from "react";
import { cleanup, render, screen } from "@testing-library/react";

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

describe("entries table conversion (product gap 8)", () => {
  it("shows the original amount and the report-currency conversion", () => {
    render(
      <EntriesTable
        entries={[
          entry({
            id: "e-eur",
            amount: "25000",
            currency: "JPY",
            amount_conversion: {
              original_amount: "25000",
              original_currency: "JPY",
              converted_amount: "167.50",
              currency: "USD",
              rate: "0.0067",
              supported: true,
            },
          }),
          entry({ id: "e-usd" }),
        ]}
      />,
    );
    const note = screen.getByTestId("entry-amount-conversion-e-eur");
    expect(note.textContent).toBe("JPY 25000 ≈ USD 167.50 at 0.0067");
    expect(note.getAttribute("title")).toContain("not live market data");
    expect(screen.queryByTestId("entry-amount-conversion-e-usd")).toBeNull();
  });
});
