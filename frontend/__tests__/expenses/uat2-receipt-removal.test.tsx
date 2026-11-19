/**
 * UAT2 browser retest (defect B) — a wrong receipt attached to a RETURNED report
 * can be removed again.
 *
 * Old behaviour: the entry editor hid "Remove" for EVERY receipt once the report
 * had any approval history (`!report.approval_status`) and said "Kept for audit"
 * about all of them — though the server lets the owner delete a receipt attached
 * after the return (nobody reviewed it). Now each receipt's Remove follows the
 * server's per-receipt `receipt_removal` flag, behind an in-page confirm, and
 * "Kept for audit" is said only about receipts that really are kept.
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
  deleteReceiptMutate: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useUpdateEntry: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({
    mutate: h.deleteReceiptMutate,
    isPending: false,
    variables: undefined,
  }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));

import { EntryEditor } from "@/components/expenses/entry-editor";
import type { ExpenseEntry, EntryReceiptRemoval } from "@/lib/types";

const KEPT =
  "This receipt was part of a submitted report and is kept for audit.";

function removal(
  receiptId: string,
  over: Partial<EntryReceiptRemoval> = {},
): EntryReceiptRemoval {
  return {
    receipt_id: receiptId,
    removable: true,
    kept_for_audit: false,
    removal_blocked_reason: null,
    ...over,
  };
}

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-meal",
    organization_id: "org",
    report_id: "rep-q3",
    trip_segment_id: null,
    category: "meals",
    amount: "60.00",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Corner Deli",
    description: null,
    status: "flagged",
    match_source: "manual",
    is_itemized: false,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    receipt_id: "r-seen",
    receipt_ids: ["r-seen", "r-wrong"],
    justification: null,
    deletable: false,
    delete_blocked_reason: null,
    receipt_removal: [
      removal("r-seen", {
        removable: false,
        kept_for_audit: true,
        removal_blocked_reason: KEPT,
      }),
      removal("r-wrong"),
    ],
    ...over,
  };
}

const RETURNED = {
  id: "rep-q3",
  version: 7,
  approval_status: "changes_requested" as const,
};

function mount(e: ExpenseEntry, report = RETURNED) {
  render(<EntryEditor entry={e} report={report} onClose={() => {}} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("removing a receipt from a returned report's line", () => {
  it("offers Remove only for the receipt the server says may go", () => {
    mount(entry());
    expect(
      screen.queryByRole("button", { name: "Remove receipt 1" }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: "Remove receipt 2" }),
    ).toBeTruthy();
    // "Kept for audit" is said about the kept receipt only.
    const kept = screen.getAllByTestId("receipt-kept-for-audit");
    expect(kept).toHaveLength(1);
    expect(kept[0].closest("li")?.textContent).toMatch("Receipt 1");
  });

  it("confirms in-page, then DELETEs from this line only", () => {
    mount(entry());
    fireEvent.click(screen.getByRole("button", { name: "Remove receipt 2" }));
    expect(h.deleteReceiptMutate).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", {
      name: "Remove this receipt?",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(h.deleteReceiptMutate).toHaveBeenCalledWith(
      { receiptId: "r-wrong", fromEntryId: "e-meal" },
      expect.anything(),
    );
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("cancelling the confirm removes nothing", () => {
    mount(entry());
    fireEvent.click(screen.getByRole("button", { name: "Remove receipt 2" }));
    const dialog = screen.getByRole("dialog", {
      name: "Remove this receipt?",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(h.deleteReceiptMutate).not.toHaveBeenCalled();
  });

  it("no 'Kept for audit' note when every receipt can still be removed", () => {
    mount(
      entry({
        receipt_ids: ["r-wrong"],
        receipt_id: "r-wrong",
        receipt_removal: [removal("r-wrong")],
      }),
    );
    expect(screen.queryByTestId("receipt-kept-for-audit")).toBeNull();
    expect(screen.queryByText(/Kept for audit/)).toBeNull();
    expect(
      screen.getByRole("button", { name: "Remove receipt 1" }),
    ).toBeTruthy();
  });

  it("an older backend without the per-receipt flag keeps the report rule", () => {
    mount(entry({ receipt_removal: undefined }));
    expect(screen.queryByRole("button", { name: /Remove receipt/ })).toBeNull();
    mount(entry({ id: "e-2", receipt_removal: undefined }), {
      ...RETURNED,
      approval_status: null,
    } as unknown as typeof RETURNED);
    expect(
      screen.getAllByRole("button", { name: /Remove receipt/ }).length,
    ).toBe(2);
  });
});
