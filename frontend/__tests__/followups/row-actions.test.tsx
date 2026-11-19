/**
 * Follow-up (UI polish) — row action buttons used to wrap onto two lines on
 * narrow widths (entries table, trip cards, users table). RowActions keeps them
 * on one line (flex-nowrap) and, below `sm`, folds them behind one toggle that
 * expands them in place (still mounted while folded).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

import {
  ROW_ACTIONS_NARROW_QUERY,
  RowActions,
} from "@/components/atlas/row-actions";
import { EntriesTable } from "@/components/expenses/entries-table";
import type { ExpenseEntry } from "@/lib/types";

function stubWidth(narrow: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: narrow && query === ROW_ACTIONS_NARROW_QUERY,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function Actions({ onEdit }: { onEdit: () => void }) {
  return (
    <RowActions label="Actions for Hotel">
      <button type="button" onClick={onEdit}>
        Edit
      </button>
      <button type="button">Delete</button>
    </RowActions>
  );
}

describe("RowActions", () => {
  it("wide: inline on one line (nowrap), no toggle", () => {
    stubWidth(false);
    render(<Actions onEdit={() => {}} />);
    const row = screen.getByTestId("row-actions");
    expect(row.className).toMatch(/\bflex-nowrap\b/);
    expect(row.className).toMatch(/\bwhitespace-nowrap\b/);
    expect(row.className).not.toMatch(/\bflex-wrap\b/);
    expect(
      screen.queryByRole("button", { name: "Actions for Hotel" }),
    ).toBeNull();
    expect(screen.getByRole("button", { name: "Edit" })).toBeTruthy();
  });

  it("without matchMedia (SSR / tests) renders inline", () => {
    render(<Actions onEdit={() => {}} />);
    expect(screen.getByTestId("row-actions").dataset.folded).toBeUndefined();
  });

  it("narrow: folded behind one toggle, expands in place, stays mounted", () => {
    stubWidth(true);
    const onEdit = vi.fn();
    render(<Actions onEdit={onEdit} />);
    const toggle = screen.getByRole("button", { name: "Actions for Hotel" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    // Folded: not exposed, but still in the DOM (parent-owned state survives).
    expect(screen.queryByRole("button", { name: "Edit" })).toBeNull();
    expect(screen.getByText("Edit")).toBeTruthy();
    const group = screen.getByRole("group", { hidden: true });
    expect(group.hidden).toBe(true);
    expect(group.className).toMatch(/\bhidden\b/);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(group.hidden).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(onEdit).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(group, { key: "Escape" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-1",
    organization_id: "org-1",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "meals",
    amount: "42.50",
    currency: "USD",
    expense_date: "2026-06-11",
    vendor: "Corner Cafe",
    description: null,
    status: "draft",
    match_source: null,
    is_itemized: false,
    created_by: "u-1",
    updated_by: "u-1",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    ...over,
  };
}

describe("entries table uses RowActions", () => {
  const rowActions = () => (
    <>
      <button type="button" aria-label="Attach receipt">
        A
      </button>
      <button type="button" aria-label="Delete entry">
        D
      </button>
    </>
  );

  it("wide: Edit + row actions on one nowrap line", () => {
    stubWidth(false);
    render(
      <EntriesTable
        entries={[entry()]}
        onEditEntry={() => {}}
        rowActions={rowActions}
      />,
    );
    const row = screen.getByTestId("row-actions");
    expect(row.className).toMatch(/\bflex-nowrap\b/);
    expect(row.querySelectorAll("button")).toHaveLength(3);
  });

  it("narrow: one toggle per row", () => {
    stubWidth(true);
    render(
      <EntriesTable
        entries={[entry()]}
        onEditEntry={() => {}}
        rowActions={rowActions}
      />,
    );
    expect(screen.getByRole("button", { name: /^Actions for / })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Delete entry" })).toBeNull();
  });
});
