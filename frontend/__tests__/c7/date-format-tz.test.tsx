import { describe, it, expect, beforeAll, afterAll, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

/**
 * C7-20: date-only "YYYY-MM-DD" values must render as the same calendar day in
 * time zones BEHIND UTC. Node honours a runtime process.env.TZ change, so these
 * tests run under America/Vancouver (UTC-7/-8) and restore the zone after.
 */
const ORIGINAL_TZ = process.env.TZ;
beforeAll(() => {
  process.env.TZ = "America/Vancouver";
});
afterAll(() => {
  if (ORIGINAL_TZ === undefined) delete process.env.TZ;
  else process.env.TZ = ORIGINAL_TZ;
});
afterEach(cleanup);

import { formatDate as formatExpenseDate } from "@/lib/expenses/format";
import { formatDate as formatApprovalDate } from "@/lib/approvals/format";
import { parseDateForDisplay } from "@/lib/dates";
import { EntryLineage } from "@/components/expenses/entry-lineage";

describe("C7-20 date-only formatting under a negative UTC offset", () => {
  it("sanity: the zone really is behind UTC", () => {
    expect(new Date(2026, 5, 10).getTimezoneOffset()).toBeGreaterThan(0);
    // The OLD behaviour this guards against: UTC-midnight parsing → Jun 9.
    expect(
      new Date("2026-06-10").toLocaleDateString("en-US", {
        month: "short",
        day: "numeric",
        year: "numeric",
      }),
    ).toBe("Jun 9, 2026");
  });

  it("expenses formatDate keeps the calendar day", () => {
    expect(formatExpenseDate("2026-06-10")).toBe("Jun 10, 2026");
    expect(formatExpenseDate("2026-01-01")).toBe("Jan 1, 2026");
    expect(formatExpenseDate("2026-12-31")).toBe("Dec 31, 2026");
  });

  it("approvals formatDate keeps the calendar day for date-only values", () => {
    expect(formatApprovalDate("2026-06-10")).toBe("Jun 10, 2026");
  });

  it("timestamps keep instant semantics (converted to local time)", () => {
    // 03:00Z on Jun 10 is still Jun 9 in Vancouver — correct for an instant.
    expect(formatExpenseDate("2026-06-10T03:00:00Z")).toBe("Jun 9, 2026");
    expect(formatApprovalDate("2026-06-10T20:00:00Z")).toBe("Jun 10, 2026");
  });

  it("invalid / impossible dates render an em dash", () => {
    expect(formatExpenseDate("2026-02-31")).toBe("—");
    expect(formatExpenseDate("not-a-date")).toBe("—");
    expect(formatExpenseDate(null)).toBe("—");
    expect(Number.isNaN(parseDateForDisplay("2026-13-01").getTime())).toBe(
      true,
    );
  });

  it("EntryLineage (report detail + approval panel) shows the stored date", () => {
    render(
      <EntryLineage
        entry={
          {
            id: "e1",
            category: "airfare",
            amount: "320.00",
            currency: "USD",
            expense_date: "2026-06-10",
            vendor: "United",
            description: null,
            match_source: null,
            receipt_id: null,
            card_transaction_id: null,
            trip_id: null,
          } as never
        }
      />,
    );
    expect(screen.getByText("Jun 10, 2026")).toBeTruthy();
    expect(screen.queryByText("Jun 9, 2026")).toBeNull();
  });
});
