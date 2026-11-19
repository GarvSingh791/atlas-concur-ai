/**
 * UAT2 browser retest (defect H) — the entry editor lists receipts the employee
 * can tell apart, and each one can be opened.
 *
 * Old behaviour: a returned meals line with three receipts showed "Receipt 1",
 * "Receipt 2", "Receipt 3" — no vendor, date or total, and no way to open any of
 * them — so the employee could not tell which one was the wrong one to remove.
 * Now each row carries what the receipt reads (server `receipts`, UAT2 defect F:
 * the machine reading the approver also sees), its review state from
 * `receipt_removal` / `attached_after_return` ("Kept for audit" vs "Not yet
 * reviewed · can be removed"),
 * and a View action that opens the receipt through the scoped signed link
 * (GET /receipts/{id}) in the existing preview, with an "Open in new tab" link.
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
  useReceipt: vi.fn(),
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
  useReceipt: h.useReceipt,
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));

import { EntryEditor } from "@/components/expenses/entry-editor";
import type {
  EntryReceipt,
  EntryReceiptRemoval,
  ExpenseEntry,
} from "@/lib/types";

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

function detail(
  receiptId: string,
  over: Partial<EntryReceipt> = {},
): EntryReceipt {
  return {
    receipt_id: receiptId,
    link: "direct",
    vendor: null,
    receipt_date: null,
    amount: null,
    currency: null,
    covers_line: null,
    attached_after_return: false,
    ...over,
  };
}

/** The seeded returned Q3 meals line: one reviewed receipt + two added since. */
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
    receipt_ids: ["r-seen", "r-right", "r-wrong"],
    justification: null,
    deletable: false,
    delete_blocked_reason: null,
    receipt_removal: [
      removal("r-seen", {
        removable: false,
        kept_for_audit: true,
        removal_blocked_reason: KEPT,
      }),
      removal("r-right"),
      removal("r-wrong"),
    ],
    receipts: [
      detail("r-seen", {
        vendor: "Corner Deli",
        receipt_date: "2026-06-10",
        amount: "60.00",
        currency: "USD",
      }),
      detail("r-right", {
        vendor: "Union Square Cafe",
        receipt_date: "2026-06-11",
        amount: "48.20",
        currency: "USD",
        attached_after_return: true,
      }),
      detail("r-wrong", {
        vendor: "Hilton Chicago",
        receipt_date: "2026-06-13",
        amount: "900.00",
        currency: "USD",
        attached_after_return: true,
      }),
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

function row(n: number): HTMLElement {
  return screen.getByTestId(`entry-receipt-${n}`);
}

const SIGNED = "https://r2.example.invalid/receipts/r-wrong.jpg?sig=abc";

beforeEach(() => {
  vi.clearAllMocks();
  h.useReceipt.mockImplementation((id: string | null) => ({
    data: id
      ? {
          id,
          image_url: `org/${id}.jpg`,
          image_access_url: SIGNED,
          image_access_expires_in: 300,
        }
      : undefined,
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  }));
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("the entry editor's receipt list (defect H)", () => {
  it("lists each receipt with its vendor, date and total", () => {
    mount(entry());
    expect(row(1).textContent).toMatch("Receipt 1");
    expect(row(1).textContent).toMatch("Corner Deli");
    expect(row(1).textContent).toMatch("Jun 10, 2026");
    expect(row(1).textContent).toMatch("$60.00");
    expect(row(2).textContent).toMatch("Union Square Cafe");
    expect(row(2).textContent).toMatch("$48.20");
    expect(row(3).textContent).toMatch("Hilton Chicago");
    expect(row(3).textContent).toMatch("Jun 13, 2026");
    expect(row(3).textContent).toMatch("$900.00");
  });

  it("shows each receipt's review state from receipt_removal", () => {
    mount(entry());
    expect(row(1).textContent).toMatch("Kept for audit");
    expect(within(row(1)).queryByRole("button", { name: /Remove/ })).toBeNull();
    for (const n of [2, 3]) {
      expect(row(n).textContent).toMatch("Not yet reviewed");
      expect(row(n).textContent).not.toMatch("Kept for audit");
      expect(
        within(row(n)).getByRole("button", { name: `Remove receipt ${n}` }),
      ).toBeTruthy();
    }
  });

  it("a never-reviewed draft's receipt carries no review badge", () => {
    mount(
      entry({
        receipt_ids: ["r-wrong"],
        receipt_id: "r-wrong",
        receipt_removal: [removal("r-wrong")],
        receipts: [detail("r-wrong", { vendor: "Hilton Chicago" })],
      }),
      { ...RETURNED, approval_status: null } as unknown as typeof RETURNED,
    );
    expect(row(1).textContent).not.toMatch("Not yet reviewed");
    expect(row(1).textContent).not.toMatch("Kept for audit");
    expect(row(1).textContent).toMatch("Hilton Chicago");
  });

  it("names the receipt in the remove confirm", () => {
    mount(entry());
    fireEvent.click(screen.getByRole("button", { name: "Remove receipt 3" }));
    const dialog = screen.getByRole("dialog", {
      name: "Remove this receipt?",
    });
    expect(dialog.textContent).toMatch("Hilton Chicago");
    expect(dialog.textContent).toMatch("$900.00");
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(h.deleteReceiptMutate).toHaveBeenCalledWith(
      { receiptId: "r-wrong", fromEntryId: "e-meal" },
      expect.anything(),
    );
  });

  it("View opens the receipt through the scoped signed link", () => {
    mount(entry());
    // Nothing is fetched (no signed link minted) until a receipt is opened.
    expect(h.useReceipt).not.toHaveBeenCalledWith("r-wrong");
    fireEvent.click(
      within(row(3)).getByRole("button", { name: "View receipt 3" }),
    );
    expect(h.useReceipt).toHaveBeenCalledWith("r-wrong");
    const dialog = screen.getByRole("dialog", { name: /Receipt 3/ });
    expect(dialog.textContent).toMatch("Hilton Chicago");
    const img = within(dialog).getByRole("img");
    expect(img.getAttribute("src")).toBe(SIGNED);
    const open = within(dialog).getByRole("link", { name: /Open in new tab/ });
    expect(open.getAttribute("href")).toBe(SIGNED);
    expect(open.getAttribute("target")).toBe("_blank");
    expect(open.getAttribute("rel")).toMatch("noopener");
    // Viewing never removes anything.
    expect(h.deleteReceiptMutate).not.toHaveBeenCalled();
  });

  it("View works for a receipt kept for audit too", () => {
    mount(entry());
    fireEvent.click(
      within(row(1)).getByRole("button", { name: "View receipt 1" }),
    );
    expect(h.useReceipt).toHaveBeenCalledWith("r-seen");
    expect(screen.getByRole("dialog", { name: /Receipt 1/ })).toBeTruthy();
  });

  it("no servable link: the preview says so and offers no new-tab link", () => {
    h.useReceipt.mockImplementation((id: string | null) => ({
      data: id
        ? { id, image_url: `org/${id}.jpg`, image_access_url: null }
        : undefined,
      isLoading: false,
      isError: false,
      refetch: vi.fn(),
    }));
    mount(entry());
    fireEvent.click(screen.getByRole("button", { name: "View receipt 2" }));
    const dialog = screen.getByRole("dialog", { name: /Receipt 2/ });
    expect(dialog.textContent).toMatch("Preview unavailable");
    expect(within(dialog).queryByRole("link")).toBeNull();
  });

  it("a receipt with nothing read still lists, with an honest note", () => {
    mount(
      entry({
        receipt_ids: ["r-blank"],
        receipt_id: "r-blank",
        receipt_removal: [removal("r-blank")],
        receipts: [detail("r-blank")],
      }),
    );
    expect(row(1).textContent).toMatch("No vendor, date or total read");
    expect(
      within(row(1)).getByRole("button", { name: "View receipt 1" }),
    ).toBeTruthy();
  });

  it("an older backend without receipts still lists and opens receipts", () => {
    mount(entry({ receipts: undefined }));
    expect(row(3).textContent).toMatch("Receipt 3");
    expect(row(3).textContent).not.toMatch("No vendor, date or total read");
    fireEvent.click(screen.getByRole("button", { name: "View receipt 3" }));
    expect(h.useReceipt).toHaveBeenCalledWith("r-wrong");
  });

  it("a receipt added after the return but not removable by this viewer still reads as not yet reviewed", () => {
    mount(
      entry({
        receipt_removal: [
          removal("r-seen", {
            removable: false,
            kept_for_audit: true,
            removal_blocked_reason: KEPT,
          }),
          removal("r-right", {
            removable: false,
            removal_blocked_reason:
              "Only the person who uploaded this receipt can remove it.",
          }),
          removal("r-wrong"),
        ],
      }),
    );
    expect(row(2).textContent).toMatch("Not yet reviewed");
    expect(row(2).textContent).not.toMatch("can be removed");
    expect(within(row(2)).queryByRole("button", { name: /Remove/ })).toBeNull();
    expect(row(3).textContent).toMatch("Not yet reviewed · can be removed");
  });
});
