/**
 * UAT2 Issues 1+2 hardening — receipt evidence can't be retyped; honest attach toast.
 *
 * Old behaviour:
 *   - a receipt backing a submitted / returned report (or kept for audit) still
 *     showed Edit controls, and the server accepted the correction;
 *   - the attach toast said "Receipt attached" / "You can still submit" from the
 *     entry's missing-receipt flag alone, though another flag (on this line or
 *     another) still blocked Submit;
 *   - an itemized folio already on one line couldn't be picked for its sibling,
 *     and "Remove receipt" on one sibling would delete it from both.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  act,
  render,
  screen,
  fireEvent,
  cleanup,
  within,
  waitFor,
} from "@testing-library/react";

const h = vi.hoisted(() => ({
  linkMutate: vi.fn(),
  attachMutate: vi.fn(),
  fetchFresh: vi.fn(),
  attachableFor: vi.fn(),
  attachable: [] as unknown[],
  showToast: vi.fn(),
  patch: vi.fn(),
  get: vi.fn(),
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useLinkReceipt: () => ({ mutate: h.linkMutate, isPending: false }),
  fetchFreshReport: h.fetchFresh,
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useAttachReceipt: () => ({ mutate: h.attachMutate, isPending: false }),
  useAttachableReceipts: (forEntryId?: string) => {
    h.attachableFor(forEntryId);
    return { data: h.attachable, isLoading: false };
  },
  useCorrectReceiptField: () => ({
    mutateAsync: h.patch,
    isPending: false,
  }),
  useReceipt: () => ({ data: undefined, isLoading: false }),
}));
vi.mock("@tanstack/react-query", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-query")>()),
  useQueryClient: () => ({}),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));
vi.mock("@/components/atlas", async (orig) => ({
  ...(await orig<typeof import("@/components/atlas")>()),
  useAtlasToast: () => ({ showToast: h.showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: h.get, patch: vi.fn(), post: vi.fn(), delete: vi.fn() },
}));

import {
  AttachReceiptDialog,
  attachOutcomeToast,
} from "@/components/expenses/attach-receipt-dialog";
import { ExtractionReview } from "@/components/receipts/extraction-review";
import { ApiError } from "@/lib/types/api";
import type { ExpenseEntry, ExpenseReport, Receipt } from "@/lib/types";

function report(over: Partial<ExpenseReport> = {}): ExpenseReport {
  return {
    id: "rep-1",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "Chicago",
    status: "draft",
    total_amount: "900.00",
    currency: "USD",
    submitted_at: null,
    pre_submit_flags: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 3,
    approval_status: null,
    ...over,
  };
}

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-food",
    organization_id: "org",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "meals",
    amount: "150.00",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Hilton Chicago",
    description: null,
    status: "flagged",
    match_source: "manual",
    is_itemized: false,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    receipt_id: null,
    receipt_ids: [],
    justification: null,
    deletable: true,
    delete_blocked_reason: null,
    ...over,
  };
}

const CLEAR_VERDICT = {
  in_policy: true,
  blocking_count: 0,
  advisory_count: 0,
  flag_types: [],
};

// The attached line is fine now, but ANOTHER line still blocks Submit.
const OTHER_LINE_BLOCKS = {
  flags: [
    {
      entry_id: "e-stay",
      flag_type: "missing_receipt",
      reason: "Receipt required for Hilton Chicago but no receipt attached.",
      severity: "blocking",
    },
  ],
};

function mountDialog(reportEntryIds: string[] = ["e-food", "e-stay"]) {
  render(
    <AttachReceiptDialog
      entry={entry()}
      report={report()}
      reportEntryIds={reportEntryIds}
      onOpenChange={() => {}}
    />,
  );
  return screen.getByRole("dialog", { name: "Attach a receipt" });
}

beforeEach(() => {
  vi.clearAllMocks();
  h.attachable = [];
  h.get.mockResolvedValue({ image_access_url: null });
});
afterEach(() => {
  cleanup();
});

describe("attach toast follows the real submit gate (decision 3)", () => {
  it("the attached line is fine but another line still blocks submitting", () => {
    const t = attachOutcomeToast(CLEAR_VERDICT, {
      pre_submit_flags: OTHER_LINE_BLOCKS,
    });
    expect(t.title).toBe(
      "Receipt attached — the report still can’t be submitted",
    );
    expect(t.description).toMatch(/1 issue still blocks submitting/);
  });

  it("says 'ready to submit' only when the refreshed gate is green", () => {
    expect(
      attachOutcomeToast(CLEAR_VERDICT, { pre_submit_flags: { flags: [] } })
        .title,
    ).toBe("Receipt attached — ready to submit");
  });

  it("advisory flags only: you can submit, the approver reviews", () => {
    const t = attachOutcomeToast(
      { ...CLEAR_VERDICT, flag_types: ["receipt_mismatch"], advisory_count: 1 },
      {
        pre_submit_flags: {
          flags: [
            {
              entry_id: "e-food",
              flag_type: "receipt_mismatch",
              severity: "advisory",
              reason: "may be the same receipt as the one on report 'June'",
            },
          ],
        },
      },
    );
    expect(t.title).toBe("Receipt attached — you can submit");
    expect(t.description).toMatch(/approver will review/);
  });

  it("never claims anything about submitting without the refreshed report", () => {
    const t = attachOutcomeToast(CLEAR_VERDICT, null);
    expect(t.title).toBe("Receipt attached");
    expect(t.description).not.toMatch(
      /can submit|ready to submit|still block/i,
    );
  });

  it("an upload fetches the report fresh and toasts from its gate", async () => {
    h.fetchFresh.mockResolvedValue(
      report({ pre_submit_flags: OTHER_LINE_BLOCKS }),
    );
    const dialog = mountDialog();
    fireEvent.change(within(dialog).getByTestId("attach-receipt-file"), {
      target: {
        files: [new File(["img"], "folio.png", { type: "image/png" })],
      },
    });
    const [, callbacks] = h.attachMutate.mock.calls[0];
    callbacks.onSuccess({ policy_verdict: CLEAR_VERDICT });

    await waitFor(() =>
      expect(h.showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Receipt attached — the report still can’t be submitted",
        }),
      ),
    );
    expect(h.fetchFresh).toHaveBeenCalledWith(expect.anything(), "rep-1");
  });

  it("phase 11: a file the server refuses is reported in the dialog, not toasted away", async () => {
    const dialog = mountDialog();
    fireEvent.change(within(dialog).getByTestId("attach-receipt-file"), {
      target: {
        files: [new File(["img"], "folio.png", { type: "image/png" })],
      },
    });
    const [, callbacks] = h.attachMutate.mock.calls[0];
    act(() =>
      callbacks.onError(
        new ApiError(
          {
            code: "INVALID_UPLOAD",
            message: "The image is damaged or incomplete and can't be read.",
            correlation_id: "cid",
          },
          422,
        ),
      ),
    );
    expect(
      await within(dialog).findByText(/damaged or incomplete/),
    ).toBeTruthy();
    expect(h.showToast).not.toHaveBeenCalled();
  });

  it("phase 11: a file of the wrong type never leaves the browser", () => {
    const dialog = mountDialog();
    fireEvent.change(within(dialog).getByTestId("attach-receipt-file"), {
      target: {
        files: [new File(["img"], "folio.jpg", { type: "image/png" })],
      },
    });
    expect(h.attachMutate).not.toHaveBeenCalled();
    expect(within(dialog).getByText(/don't match/)).toBeTruthy();
  });

  it("a link uses the re-checked report the server returned", async () => {
    h.attachable = [
      {
        id: "r-1",
        expense_id: null,
        extracted_fields: { vendor: "Deli", amount: 150, currency: "USD" },
      },
    ];
    const dialog = mountDialog();
    fireEvent.click(within(dialog).getByRole("radio"));
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Attach selected" }),
    );
    const [, callbacks] = h.linkMutate.mock.calls[0];
    callbacks.onSuccess({
      policy_verdict: CLEAR_VERDICT,
      report: report({ pre_submit_flags: { flags: [] } }),
    });

    await waitFor(() =>
      expect(h.showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: "Receipt attached — ready to submit",
        }),
      ),
    );
    expect(h.fetchFresh).not.toHaveBeenCalled();
  });
});

describe("itemized siblings (decision 4)", () => {
  it("asks the server for receipts on this report's other lines and labels them", () => {
    h.attachable = [
      {
        id: "r-folio",
        expense_id: "e-stay",
        extracted_fields: { vendor: "Hilton Chicago", amount: 900 },
      },
    ];
    const dialog = mountDialog();
    expect(h.attachableFor).toHaveBeenCalledWith("e-food");
    expect(
      within(dialog).getByText(/On another line of this report/),
    ).toBeTruthy();
    expect(within(dialog).queryByText(/From your Receipt Inbox/)).toBeNull();
  });

  it("offers a folio already split onto this line so a stale flag can be cleared", () => {
    h.attachable = [
      {
        id: "r-folio",
        expense_id: "e-stay",
        shared_expense_ids: ["e-food"],
        extracted_fields: { vendor: "Hilton Chicago", amount: 900 },
      },
    ];
    const dialog = mountDialog();
    expect(
      within(dialog).getByText(/Already split onto this line/),
    ).toBeTruthy();
    expect(
      within(dialog).queryByText(/On another line of this report/),
    ).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Decision 1 — relied-on evidence can't be edited
// ---------------------------------------------------------------------------

function receipt(over: Partial<Receipt> = {}): Receipt {
  return {
    id: "rcpt-1",
    organization_id: "org",
    uploaded_by: "emp",
    expense_id: "e-food",
    image_url: "org/abc",
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: {
      amount: 60,
      currency: "USD",
      date: "2026-06-10",
      vendor: "Corner Deli",
      location: "New York, NY",
      expense_type: "meals",
    },
    field_confidence: {
      amount: 0.95,
      currency: 0.99,
      date: 0.95,
      vendor: 0.93,
      location: 0.9,
      expense_type: 0.92,
    },
    field_provenance: {
      amount: "deterministic",
      currency: "deterministic",
      date: "deterministic",
      vendor: "deterministic",
      location: "deterministic",
      expense_type: "deterministic",
    },
    created_at: "2026-06-10T00:00:00Z",
    ...over,
  };
}

describe("a locked receipt can't be edited (decision 1)", () => {
  it("hides every Edit control and explains why", () => {
    render(
      <ExtractionReview
        receipt={receipt({
          correction_locked: true,
          correction_locked_reason:
            "This receipt is part of a submitted report and can't be changed — attach a new receipt instead.",
        })}
        previewUrl=""
        fileName="r.png"
        onReceiptUpdate={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: /^Edit / })).toBeNull();
    expect(screen.getByTestId("receipt-locked-notice").textContent).toMatch(
      /part of a submitted report/,
    );
  });

  it("an unlocked receipt keeps its Edit controls", () => {
    render(
      <ExtractionReview
        receipt={receipt()}
        previewUrl=""
        fileName="r.png"
        onReceiptUpdate={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Edit Vendor" })).toBeTruthy();
    expect(screen.queryByTestId("receipt-locked-notice")).toBeNull();
  });

  it("a 409 RECEIPT_LOCKED (submitted meanwhile) locks the review in place", async () => {
    const onUpdate = vi.fn();
    h.patch.mockRejectedValue(
      new ApiError(
        {
          code: "RECEIPT_LOCKED",
          message:
            "This receipt is part of a submitted report and can't be changed — attach a new receipt instead.",
          correlation_id: "c",
        },
        409,
      ),
    );
    render(
      <ExtractionReview
        receipt={receipt()}
        previewUrl=""
        fileName="r.png"
        onReceiptUpdate={onUpdate}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit Vendor" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Vendor value" }), {
      target: { value: "SkyPark" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save Vendor" }));

    await waitFor(() =>
      expect(onUpdate).toHaveBeenCalledWith(
        expect.objectContaining({ correction_locked: true }),
      ),
    );
    expect(h.showToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "This receipt can’t be changed" }),
    );
  });
});
