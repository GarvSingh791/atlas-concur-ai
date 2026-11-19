/**
 * UAT2 defect F — the approver sees EVERY receipt backing a line.
 *
 * Browser evidence: a resubmitted report's meals line carried a wrong $8.75
 * coffee receipt (attached first) and the correct $205.50 receipt (attached
 * after the return); the approval detail showed only the earliest one, so the
 * approver could not see the receipt that actually supports the claim.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));

import { api } from "@/lib/api/client";
import { DecisionPanel } from "@/components/approvals/decision-panel";
import { EntryLineage } from "@/components/expenses/entry-lineage";
import type { ApprovalDetail } from "@/lib/types/approvals";
import type { EntryReceipt, ExpenseEntry, Receipt } from "@/lib/types";

const getMock = api.get as unknown as ReturnType<typeof vi.fn>;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function signed(id: string): Receipt {
  return {
    id,
    organization_id: "org",
    uploaded_by: "u-sub",
    expense_id: "e-meal",
    image_url: `org/${id}`,
    ocr_status: "processed",
    processing_path: "deterministic",
    extracted_fields: null,
    field_confidence: null,
    field_provenance: null,
    created_at: "2026-06-11T10:00:00Z",
    image_access_url: `https://signed.test/${id}?sig=1`,
    image_access_expires_at: new Date(Date.now() + 300_000).toISOString(),
    image_access_expires_in: 300,
  };
}

const WRONG: EntryReceipt = {
  receipt_id: "rcpt-wrong",
  link: "direct",
  vendor: "Blue Bottle Coffee",
  receipt_date: "2026-06-10",
  amount: "8.75",
  currency: "USD",
  covers_line: false,
  attached_after_return: false,
};
const RIGHT: EntryReceipt = {
  receipt_id: "rcpt-right",
  link: "direct",
  vendor: "Gramercy Tavern",
  receipt_date: "2026-06-10",
  amount: "205.50",
  currency: "USD",
  covers_line: true,
  attached_after_return: true,
};

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-meal",
    organization_id: "org",
    report_id: "r1",
    trip_segment_id: null,
    category: "meals",
    amount: "205.50",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Gramercy Tavern",
    description: null,
    status: "draft",
    match_source: "manual",
    is_itemized: false,
    created_by: "u-sub",
    updated_by: "u-sub",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    receipt_id: "rcpt-wrong",
    receipt_ids: ["rcpt-wrong", "rcpt-right"],
    receipts: [WRONG, RIGHT],
    ...over,
  };
}

function detail(entries: ExpenseEntry[]): ApprovalDetail {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u-sub",
    submitter_name: "Alex",
    current_status: "pending",
    is_auto_approved: false,
    risk_summary: {
      route: "escalate",
      subject_in_policy: true,
      blocking_flag_count: 0,
      flags: [],
    },
    risk_level: "medium",
    version: 2,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    steps: [],
    expense_report: {
      report: {
        id: "r1",
        organization_id: "org",
        owner_id: "u-sub",
        trip_id: null,
        title: "Q3",
        status: "submitted",
        total_amount: "205.50",
        currency: "USD",
        submitted_at: "2026-06-12T00:00:00Z",
        pre_submit_flags: null,
        created_by: null,
        updated_by: null,
        created_at: "2026-06-12T00:00:00Z",
        updated_at: "2026-06-12T00:00:00Z",
        version: 2,
      },
      entries,
    },
    trip: null,
  };
}

function wrap(node: React.ReactNode) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

function receiptCard(name: RegExp): HTMLElement {
  return screen.getByRole("group", { name });
}

describe("UAT2 F — approval detail shows every receipt on a line", () => {
  it("expanding a line shows BOTH receipts, each with its signed image and machine reading", async () => {
    getMock.mockImplementation((path: string) =>
      Promise.resolve(signed(path.replace("/receipts/", ""))),
    );
    wrap(
      <DecisionPanel
        approvalId="ap-1"
        detail={detail([entry()])}
        isLoading={false}
        isError={false}
        onRetry={() => {}}
        onBack={() => {}}
        canDecide
        currentUserId="u-mgr"
      />,
    );
    const row = screen
      .getAllByText("Gramercy Tavern")
      .map((el) => el.closest("tr"))
      .find((r): r is HTMLTableRowElement => r !== null);
    expect(row).toBeTruthy();
    // The collapsed row already says there is more than one receipt.
    expect(within(row as HTMLElement).getByText("2 receipts")).toBeTruthy();

    fireEvent.click(
      within(row as HTMLElement).getByRole("button", { name: "Expand row" }),
    );

    expect(screen.getByText("Receipts (2)")).toBeTruthy();
    const wrong = receiptCard(/Blue Bottle Coffee/);
    const right = receiptCard(/Gramercy Tavern/);
    expect(within(wrong).getByText("$8.75")).toBeTruthy();
    expect(within(wrong).getByText("Doesn’t cover this line")).toBeTruthy();
    expect(within(wrong).queryByText("Added after the last return")).toBeNull();
    expect(within(right).getByText("$205.50")).toBeTruthy();
    expect(within(right).getByText("Added after the last return")).toBeTruthy();
    expect(within(right).queryByText("Doesn’t cover this line")).toBeNull();

    // Each opens through its own scoped, short-lived link (GET /receipts/{id}).
    await waitFor(() => {
      expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-wrong");
      expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-right");
    });
    const wrongImg = (await within(wrong).findByRole(
      "img",
    )) as HTMLImageElement;
    const rightImg = (await within(right).findByRole(
      "img",
    )) as HTMLImageElement;
    expect(wrongImg.getAttribute("src")).toBe(
      "https://signed.test/rcpt-wrong?sig=1",
    );
    expect(rightImg.getAttribute("src")).toBe(
      "https://signed.test/rcpt-right?sig=1",
    );
  });

  it("marks a folio shared from an itemized sibling and an unread total", () => {
    getMock.mockImplementation((path: string) =>
      Promise.resolve(signed(path.replace("/receipts/", ""))),
    );
    wrap(
      <EntryLineage
        entry={entry({
          receipt_id: "rcpt-typed",
          receipt_ids: ["rcpt-typed", "rcpt-folio"],
          receipts: [
            {
              receipt_id: "rcpt-typed",
              link: "direct",
              vendor: "Room Service",
              receipt_date: null,
              amount: null,
              currency: "USD",
              covers_line: null,
              attached_after_return: false,
            },
            {
              receipt_id: "rcpt-folio",
              link: "shared",
              vendor: "Hilton Chicago",
              receipt_date: "2026-06-13",
              amount: "900.00",
              currency: "USD",
              covers_line: true,
              attached_after_return: false,
            },
          ],
        })}
      />,
    );
    const typed = receiptCard(/Room Service/);
    expect(within(typed).getByText("Total not machine-read")).toBeTruthy();
    expect(within(typed).queryByText("Shared with another line")).toBeNull();
    const folio = receiptCard(/Hilton Chicago/);
    expect(within(folio).getByText("Shared with another line")).toBeTruthy();
    expect(within(folio).getByText("$900.00")).toBeTruthy();
    expect(within(folio).getByText("Jun 13, 2026")).toBeTruthy();
  });

  it("an older backend without `receipts` still shows every receipt id", async () => {
    getMock.mockImplementation((path: string) =>
      Promise.resolve(signed(path.replace("/receipts/", ""))),
    );
    wrap(<EntryLineage entry={entry({ receipts: undefined })} />);
    await waitFor(() => {
      expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-wrong");
      expect(getMock).toHaveBeenCalledWith("/receipts/rcpt-right");
    });
    expect(screen.getByText("Receipts (2)")).toBeTruthy();
  });
});
