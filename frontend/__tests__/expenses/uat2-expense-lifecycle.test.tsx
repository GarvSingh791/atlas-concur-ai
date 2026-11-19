/**
 * UAT2 Issues 1+2 — attach a receipt to an entry; delete entries / receipts /
 * never-submitted drafts.
 *
 * Old behaviour: a returned report's "missing receipt" flag offered only "Edit
 * entry" (no way to attach), nothing could be deleted (the receipts-screen X only
 * dropped a local list item), a returned report listed every issue twice, and an
 * empty report could be submitted.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
  waitFor,
} from "@testing-library/react";

const h = vi.hoisted(() => ({
  useExpenseReport: vi.fn(),
  useReportEntries: vi.fn(),
  deleteEntryMutate: vi.fn(),
  deleteReportMutate: vi.fn(),
  linkMutate: vi.fn(),
  attachMutate: vi.fn(),
  deleteReceiptMutate: vi.fn(),
  submitMutate: vi.fn(),
  forget: vi.fn(),
  attachable: [] as unknown[],
  showToast: vi.fn(),
  session: { user: { id: "emp" } as { id: string } | null },
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useExpenseReport: h.useExpenseReport,
  useReportEntries: h.useReportEntries,
  useSubmitReport: () => ({ mutate: h.submitMutate, isPending: false }),
  useUpdateEntry: () => ({ mutate: vi.fn(), isPending: false }),
  useRenameReport: () => ({ mutate: vi.fn(), isPending: false }),
  useMoveEntries: () => ({ mutate: vi.fn(), isPending: false }),
  useCreateReport: () => ({ mutate: vi.fn(), isPending: false }),
  useExpenseReports: () => ({ data: { items: [] }, isLoading: false }),
  useDeleteEntry: () => ({ mutate: h.deleteEntryMutate, isPending: false }),
  useDeleteReport: () => ({ mutate: h.deleteReportMutate, isPending: false }),
  useLinkReceipt: () => ({ mutate: h.linkMutate, isPending: false }),
  forgetDeletedReport: h.forget,
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useAttachReceipt: () => ({ mutate: h.attachMutate, isPending: false }),
  useAttachableReceipts: () => ({ data: h.attachable, isLoading: false }),
  useDeleteReceipt: () => ({
    mutate: h.deleteReceiptMutate,
    isPending: false,
  }),
}));
vi.mock("@tanstack/react-query", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-query")>()),
  useQueryClient: () => ({}),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => h.session,
}));

import { ReportDetail } from "@/components/expenses/report-detail";
import { ReadinessPanel } from "@/components/expenses/readiness-panel";
import { RejectionNotice } from "@/components/expenses/rejection-notice";
import { attachOutcomeToast } from "@/components/expenses/attach-receipt-dialog";
import { ReceiptQueue } from "@/components/receipts/receipt-queue";
import { normalizePreSubmitFlags } from "@/lib/expenses/readiness";
import { ApiError } from "@/lib/types/api";
import type { ExpenseEntry, ExpenseReport, Receipt } from "@/lib/types";
import type { QueueItem } from "@/lib/receipts/use-upload-queue";

function report(over: Partial<ExpenseReport> = {}): ExpenseReport {
  return {
    id: "rep-q3",
    organization_id: "org",
    owner_id: "emp",
    trip_id: null,
    title: "Q3 client visit",
    status: "draft",
    total_amount: "222.00",
    currency: "USD",
    submitted_at: null,
    pre_submit_flags: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    version: 5,
    approval_status: null,
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
    receipt_id: null,
    receipt_ids: [],
    justification: null,
    deletable: true,
    delete_blocked_reason: null,
    ...over,
  };
}

const MISSING = {
  flags: [
    {
      entry_ref: "Corner Deli",
      entry_id: "e-meal",
      flag_type: "missing_receipt",
      source_clause_ref: "RECEIPT-REQ",
      reason: "Receipt required for Corner Deli but no receipt attached.",
      severity: "blocking",
    },
  ],
  blocking_count: 1,
  advisory_count: 0,
};

const DUPLICATE = {
  flags: [
    {
      entry_ref: "Corner Deli",
      entry_id: "e-meal",
      flag_type: "possible_duplicate",
      reason:
        "Possible duplicate claim for Corner Deli: a line with the same vendor, date and amount is already claimed on report 'Inbox' (draft).",
      severity: "blocking",
    },
  ],
  blocking_count: 1,
  advisory_count: 0,
};

function mount(
  r: ExpenseReport,
  entries: ExpenseEntry[] = [entry()],
  onBack = vi.fn(),
) {
  h.useExpenseReport.mockReturnValue({
    isLoading: false,
    isError: false,
    data: r,
    refetch: vi.fn(),
  });
  h.useReportEntries.mockReturnValue({
    isLoading: false,
    isError: false,
    data: entries,
    refetch: vi.fn(),
  });
  render(<ReportDetail reportId={r.id} onBack={onBack} />);
  return onBack;
}

beforeEach(() => {
  vi.clearAllMocks();
  h.session.user = { id: "emp" };
  h.attachable = [];
  vi.spyOn(window, "confirm").mockImplementation(() => {
    throw new Error("window.confirm must not be used");
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// Issue 1 — attach a receipt
// ---------------------------------------------------------------------------

describe("attach a receipt", () => {
  it("a missing-receipt flag offers 'Attach receipt' on the owner's draft", () => {
    mount(report({ pre_submit_flags: MISSING }));
    const panel = screen.getByText("Pre-submit check").closest("div[class]")!
      .parentElement!.parentElement!;
    fireEvent.click(
      within(panel).getByRole("button", { name: /attach receipt/i }),
    );
    expect(
      screen.getByRole("dialog", { name: "Attach a receipt" }),
    ).toBeTruthy();
  });

  it("uploads the chosen file onto the entry with the report version", async () => {
    mount(report({ pre_submit_flags: MISSING }));
    fireEvent.click(
      screen.getByRole("button", { name: "Attach receipt to Corner Deli" }),
    );
    const input = screen.getByTestId("attach-receipt-file");
    const file = new File(["img"], "deli.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    expect(h.attachMutate).toHaveBeenCalledTimes(1);
    const [args, callbacks] = h.attachMutate.mock.calls[0];
    expect(args).toMatchObject({
      entryId: "e-meal",
      reportId: "rep-q3",
      reportVersion: 5,
      file,
    });
    expect(typeof args.idempotencyKey).toBe("string");
    callbacks.onSuccess({
      policy_verdict: {
        in_policy: true,
        blocking_count: 0,
        advisory_count: 0,
        flag_types: [],
      },
    });
    // The refreshed report could not be fetched here → a neutral toast.
    await waitFor(() =>
      expect(h.showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "success",
          title: "Receipt attached",
        }),
      ),
    );
  });

  it("rejects an unsupported file client-side (nothing is sent)", () => {
    mount(report({ pre_submit_flags: MISSING }));
    fireEvent.click(
      screen.getByRole("button", { name: "Attach receipt to Corner Deli" }),
    );
    fireEvent.change(screen.getByTestId("attach-receipt-file"), {
      target: { files: [new File(["x"], "notes.txt", { type: "text/plain" })] },
    });
    expect(h.attachMutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toMatch(/unsupported file/i);
  });

  it("can attach one of the user's unattached receipts instead", () => {
    h.attachable = [
      {
        id: "r-1",
        extracted_fields: {
          vendor: "Corner Deli",
          date: "2026-06-10",
          amount: 60,
          currency: "USD",
        },
      },
    ];
    mount(report({ pre_submit_flags: MISSING }));
    fireEvent.click(
      screen.getByRole("button", { name: "Attach receipt to Corner Deli" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Attach a receipt" });
    fireEvent.click(within(dialog).getByRole("radio"));
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Attach selected" }),
    );
    expect(h.linkMutate).toHaveBeenCalledWith(
      {
        entryId: "e-meal",
        reportId: "rep-q3",
        reportVersion: 5,
        receiptId: "r-1",
      },
      expect.anything(),
    );
  });

  it("lists a Receipts-screen upload still on its inbox line, with a hint", () => {
    h.attachable = [
      {
        id: "r-inbox",
        expense_id: "e-inbox-line",
        extracted_fields: { vendor: "Metro", amount: 12, currency: "USD" },
      },
    ];
    mount(report({ pre_submit_flags: MISSING }));
    fireEvent.click(
      screen.getByRole("button", { name: "Attach receipt to Corner Deli" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Attach a receipt" });
    expect(within(dialog).getByRole("radio")).toBeTruthy();
    expect(within(dialog).getByText(/From your Receipt Inbox/)).toBeTruthy();
  });

  it("toast copy follows the server's refreshed verdict for the entry", () => {
    expect(attachOutcomeToast(null, null).title).toBe("Receipt attached");
    expect(
      attachOutcomeToast(
        {
          in_policy: false,
          blocking_count: 1,
          advisory_count: 0,
          flag_types: ["missing_receipt"],
        },
        null,
      ).title,
    ).toMatch(/doesn’t satisfy the receipt rule/);
    const advisory = {
      flags: [
        {
          entry_id: "e-meal",
          flag_type: "missing_receipt",
          reason: "Receipt attached but its total could not be verified.",
          severity: "advisory",
        },
      ],
    };
    const toast = attachOutcomeToast(
      {
        in_policy: false,
        blocking_count: 0,
        advisory_count: 1,
        flag_types: ["missing_receipt"],
      },
      { pre_submit_flags: advisory },
    );
    expect(toast.title).toMatch(/you can submit/);
    expect(toast.description).toMatch(/approver will review/);
  });
});

// ---------------------------------------------------------------------------
// Issue 2 — deletes
// ---------------------------------------------------------------------------

describe("delete an entry", () => {
  it("confirms in an in-page dialog and DELETEs with the report version", () => {
    mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Delete Corner Deli" }));
    const dialog = screen.getByRole("dialog", { name: "Delete this entry?" });
    expect(dialog.textContent).toMatch("Its receipt is deleted too");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete entry" }),
    );
    expect(h.deleteEntryMutate).toHaveBeenCalledWith(
      { entryId: "e-meal", reportId: "rep-q3", reportVersion: 5 },
      expect.anything(),
    );
    expect(window.confirm).not.toHaveBeenCalled();
  });

  it("is not offered for an entry the server won't delete", () => {
    mount(report({ approval_status: "changes_requested" }), [
      entry({
        deletable: false,
        delete_blocked_reason: "This entry was on the report ...",
      }),
    ]);
    expect(
      screen.queryByRole("button", { name: "Delete Corner Deli" }),
    ).toBeNull();
  });

  it("shows the server's refusal message", () => {
    mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Delete Corner Deli" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete entry" }));
    const [, callbacks] = h.deleteEntryMutate.mock.calls[0];
    callbacks.onError(
      new ApiError(
        {
          code: "REPORT_UNDER_REVIEW",
          message: "Keep it, fix it and resubmit.",
          correlation_id: "c",
        },
        409,
      ),
    );
    expect(h.showToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: "error",
        description: "Keep it, fix it and resubmit.",
      }),
    );
  });

  it("a duplicate flag offers 'Delete entry'", () => {
    mount(report({ pre_submit_flags: DUPLICATE }));
    fireEvent.click(screen.getByRole("button", { name: /^delete entry$/i }));
    expect(
      screen.getByRole("dialog", { name: "Delete this entry?" }),
    ).toBeTruthy();
  });

  it("no attach / delete controls for a read-only viewer", () => {
    h.session.user = { id: "manager" };
    mount(report({ pre_submit_flags: MISSING }));
    expect(
      screen.queryByRole("button", { name: /attach receipt/i }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });
});

describe("delete a report", () => {
  it("is offered only for a never-submitted draft and returns to the list", () => {
    const onBack = mount(report());
    fireEvent.click(screen.getByRole("button", { name: "Delete report" }));
    const dialog = screen.getByRole("dialog", {
      name: "Delete “Q3 client visit”?",
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Delete report" }),
    );
    const [args, callbacks] = h.deleteReportMutate.mock.calls[0];
    expect(args).toEqual({ id: "rep-q3", version: 5 });
    callbacks.onSuccess(null);
    expect(onBack).toHaveBeenCalled();
    // The detail is dropped AFTER navigating away (no 404 flash).
    expect(h.forget).toHaveBeenCalledWith(expect.anything(), "rep-q3");
  });

  it("is hidden once the report has been through approval", () => {
    mount(report({ approval_status: "changes_requested" }));
    expect(screen.queryByRole("button", { name: "Delete report" })).toBeNull();
  });
});

describe("receipts screen X", () => {
  const item = (over: Partial<QueueItem>): QueueItem =>
    ({
      id: "q1",
      fileName: "deli.png",
      status: "processed",
      receipt: { id: "r-1" } as Receipt,
      ...over,
    }) as QueueItem;

  it("is a real delete for a processed upload, a local remove otherwise", () => {
    const onDelete = vi.fn();
    const onRemove = vi.fn();
    render(
      <ReceiptQueue
        items={[
          item({}),
          item({
            id: "q2",
            fileName: "bad.png",
            status: "failed",
            receipt: undefined,
          }),
        ]}
        selectedId={null}
        onSelect={vi.fn()}
        onRetry={vi.fn()}
        onRemove={onRemove}
        onDelete={onDelete}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete deli.png" }));
    expect(onDelete).toHaveBeenCalledWith("q1");
    fireEvent.click(screen.getByRole("button", { name: "Remove bad.png" }));
    expect(onRemove).toHaveBeenCalledWith("q2");
  });
});

// ---------------------------------------------------------------------------
// M1 / M12
// ---------------------------------------------------------------------------

describe("returned report lists its issues once", () => {
  it("the notice points at the pre-submit check instead of repeating it", () => {
    mount(
      report({
        approval_status: "changes_requested",
        approval_reason: "Attach receipts.",
        pre_submit_flags: MISSING,
      }),
    );
    expect(
      screen.getAllByText(
        "Receipt required for Corner Deli but no receipt attached.",
      ),
    ).toHaveLength(1);
    expect(
      screen.getByText(
        "Fix the items in the pre-submit check below, then resubmit.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/No reason was recorded/)).toBeNull();
  });

  it("RejectionNotice still lists flags by default", () => {
    render(
      <RejectionNotice
        readiness={normalizePreSubmitFlags(MISSING)}
        reviewerReason={null}
        ownerView
      />,
    );
    expect(
      screen.getByText(
        "Receipt required for Corner Deli but no receipt attached.",
      ),
    ).toBeTruthy();
  });
});

describe("empty reports", () => {
  it("Submit is disabled with 'Add at least one expense'", () => {
    mount(report(), []);
    expect(screen.getByText("Add at least one expense")).toBeTruthy();
    expect(screen.getByText("Submit report").closest("button")).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("a returned report whose lines were all deleted can still be resubmitted", () => {
    mount(report({ approval_status: "changes_requested" }), []);
    expect(screen.queryByText("Add at least one expense")).toBeNull();
    expect(screen.getByText("Submit report").closest("button")).toHaveProperty(
      "disabled",
      false,
    );
  });
});

describe("readiness panel actions", () => {
  it("offers Delete for a duplicate only when the entry is deletable", () => {
    render(
      <ReadinessPanel
        readiness={normalizePreSubmitFlags(DUPLICATE)}
        onDeleteEntry={vi.fn()}
        canDeleteEntry={() => false}
      />,
    );
    expect(screen.queryByRole("button", { name: /delete entry/i })).toBeNull();
  });
});
