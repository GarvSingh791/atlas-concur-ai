/**
 * Product gap 2, stage 2 — process & timing rules in the UI.
 *
 *  - EXP-04: the entry editor offers the missing-receipt affidavit (clause text
 *    shown) only on a line without a receipt whose receipt the server says is
 *    required (integration: a missing_receipt flag with affidavit_allowed), and
 *    PATCHes `receipt_affidavit` only when the owner changed it.
 *  - Planner: PTA-02 / AIR-06 / AIR-04 notices are rendered on the option card
 *    at selection time, clause-cited.
 *  - Readiness hints for the new flag types (late submission, unreconciled card,
 *    accepted / refused affidavit).
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const h = vi.hoisted(() => ({ updateMutate: vi.fn(), showToast: vi.fn() }));

vi.mock("@/lib/query/expense-hooks", () => ({
  useUpdateEntry: () => ({ mutate: h.updateMutate, isPending: false }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));

import {
  AFFIDAVIT_CLAUSE,
  EntryEditor,
  diffEntryChanges,
} from "@/components/expenses/entry-editor";
import { OptionCard } from "@/components/planner/option-card";
import type { ReadinessFlag } from "@/lib/expenses/readiness";
import {
  normalizePreSubmitFlags,
  suggestedFix,
} from "@/lib/expenses/readiness";
import type { ExpenseEntry, ShortlistOption } from "@/lib/types";

afterEach(cleanup);
beforeEach(() => h.updateMutate.mockReset());

const ENTRY: ExpenseEntry = {
  id: "e1",
  organization_id: "o1",
  report_id: "r1",
  trip_segment_id: null,
  category: "meals",
  amount: "60.00",
  currency: "USD",
  expense_date: "2026-11-10",
  vendor: "Corner Cafe",
  description: null,
  status: "flagged",
  match_source: "manual",
  is_itemized: false,
  justification: null,
  receipt_affidavit_at: null,
  created_by: null,
  updated_by: null,
  created_at: "2026-11-10T00:00:00Z",
  updated_at: "2026-11-10T00:00:00Z",
  receipt_id: null,
  receipt_ids: [],
  receipts: [],
} as ExpenseEntry;

// The server's flag on a line whose receipt is required and missing (EXP-04 may
// stand in for it): the editor offers the affidavit only where it says so.
const MISSING: ReadinessFlag = {
  severity: "issue",
  message: "Receipt required … sign the missing-receipt affidavit",
  clauseRef: "RECEIPT-REQ",
  entryRef: "Corner Cafe",
  flagType: "missing_receipt",
  entryId: "e1",
  affidavitAllowed: true,
};

function editor(entry: ExpenseEntry, flags: ReadinessFlag[] = [MISSING]) {
  return render(
    <EntryEditor
      entry={entry}
      report={{ id: "r1", version: 3 }}
      flags={flags}
      onClose={() => {}}
    />,
  );
}

describe("EXP-04 affidavit in the entry editor", () => {
  it("shows the clause and sends receipt_affidavit with the explanation", () => {
    editor(ENTRY);
    const box = screen.getByTestId("entry-receipt-affidavit");
    expect(box.textContent).toContain(AFFIDAVIT_CLAUSE);
    fireEvent.change(screen.getByLabelText("Justification"), {
      target: { value: "Receipt lost in a taxi" },
    });
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(h.updateMutate).toHaveBeenCalledTimes(1);
    const [vars] = h.updateMutate.mock.calls[0];
    expect(vars.changes).toEqual({
      justification: "Receipt lost in a taxi",
      receipt_affidavit: true,
    });
    expect(vars.reportVersion).toBe(3);
  });

  it("is not offered on a line that has a receipt", () => {
    editor({
      ...ENTRY,
      receipt_id: "rc1",
      receipt_ids: ["rc1"],
      receipts: undefined,
    });
    expect(screen.queryByTestId("entry-receipt-affidavit")).toBeNull();
  });

  it("is not offered where no receipt is required (no missing_receipt flag)", () => {
    // Integration (EXP-04 blocker): a $10 coffee needs no receipt, so an
    // affidavit there replaces nothing and must not use up a quarterly slot.
    editor({ ...ENTRY, amount: "10.00" }, []);
    expect(screen.queryByTestId("entry-receipt-affidavit")).toBeNull();
  });

  it("is not offered where the server says the affidavit cannot be used", () => {
    editor({ ...ENTRY, amount: "80.00" }, [
      { ...MISSING, affidavitAllowed: false },
    ]);
    expect(screen.queryByTestId("entry-receipt-affidavit")).toBeNull();
  });

  it("reads affidavit_allowed from the server flag", () => {
    const summary = normalizePreSubmitFlags({
      flags: [
        {
          flag_type: "missing_receipt",
          severity: "blocking",
          reason: "r",
          entry_id: "e1",
          observed: { affidavit_allowed: true },
        },
        { flag_type: "missing_receipt", severity: "blocking", reason: "r" },
      ],
    });
    expect(summary.flags.map((f) => f.affidavitAllowed)).toEqual([
      true,
      undefined,
    ]);
  });

  it("stays visible to withdraw once a receipt is attached to a signed line", () => {
    editor({
      ...ENTRY,
      receipt_affidavit_at: "2026-11-11T09:00:00Z",
      receipt_id: "rc1",
      receipt_ids: ["rc1"],
      receipts: undefined,
    });
    expect(screen.getByTestId("entry-receipt-affidavit")).toBeTruthy();
    expect(
      screen.getByTestId("entry-receipt-affidavit-receipted").textContent,
    ).toMatch(/does not count toward the quarter/);
    const box = screen.getByRole("checkbox") as HTMLInputElement;
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    expect(h.updateMutate).toHaveBeenCalledTimes(1);
    const [vars] = h.updateMutate.mock.calls[0];
    expect(vars.changes).toEqual({ receipt_affidavit: false });
  });

  it("unticking withdraws a signed affidavit; untouched sends nothing", () => {
    const signed = { ...ENTRY, receipt_affidavit_at: "2026-11-11T09:00:00Z" };
    const base = {
      amount: "60.00",
      vendor: "Corner Cafe",
      category: "meals" as const,
      expenseDate: "2026-11-10",
      description: "",
      justification: "",
    };
    expect(diffEntryChanges(signed, { ...base, affidavit: true })).toEqual({});
    expect(diffEntryChanges(signed, { ...base, affidavit: false })).toEqual({
      receipt_affidavit: false,
    });
  });
});

const FLIGHT: ShortlistOption = {
  rank: 1,
  option_id: "f1",
  segment_type: "flight",
  supplier: "United Airlines",
  amount_usd: 320,
  sustainability_score: 3,
  cabin_class: "economy",
  duration_hours: 5.5,
  is_direct: true,
  city: null,
  nightly_rate_usd: null,
  room_type: null,
  in_policy: true,
  policy_label: "In Policy",
  policy_reason: null,
  clause_refs: ["AIR-CABIN", "PTA-02"],
  requires_approver: false,
  explanation: "Direct flight.",
};

describe("planner notices", () => {
  it("renders each clause-cited notice", () => {
    render(
      <OptionCard
        option={{
          ...FLIGHT,
          policy_notices: [
            {
              clause_ref: "PTA-02",
              flag_type: "approval_required",
              message:
                "Booked 5 days ahead of departure (2026-11-19); PTA-02 requires domestic flights to be booked at least 14 days ahead.",
              requires_approver: "manager",
              blocking: false,
            },
          ],
        }}
        isSelected={false}
        onSelect={() => {}}
      />,
    );
    const [n] = screen.getAllByTestId("option-policy-notice");
    expect(n.textContent).toContain("PTA-02:");
    expect(n.textContent).toContain("Booked 5 days ahead");
    expect(n.textContent).toContain("at least 14 days ahead");
  });

  it("renders nothing for an older shortlist without notices", () => {
    render(
      <OptionCard option={FLIGHT} isSelected={false} onSelect={() => {}} />,
    );
    expect(screen.queryByTestId("option-policy-notice")).toBeNull();
  });
});

function flag(over: Partial<ReadinessFlag>): ReadinessFlag {
  return {
    severity: "review",
    message: "m",
    clauseRef: null,
    entryRef: null,
    flagType: null,
    ...over,
  };
}

describe("readiness hints", () => {
  it("names the next step for the new flag types", () => {
    expect(suggestedFix(flag({ flagType: "late_submission" }))).toMatch(
      /60 days/,
    );
    expect(suggestedFix(flag({ flagType: "unreconciled_card" }))).toMatch(
      /card transactions/,
    );
    expect(
      suggestedFix(flag({ flagType: "missing_receipt", clauseRef: "EXP-04" })),
    ).toMatch(/Affidavit signed/);
    expect(
      suggestedFix(
        flag({
          flagType: "missing_receipt",
          clauseRef: "EXP-04",
          severity: "issue",
        }),
      ),
    ).toMatch(/affidavit can’t be used/);
    expect(
      suggestedFix(
        flag({
          flagType: "missing_receipt",
          clauseRef: "RECEIPT-REQ",
          severity: "issue",
        }),
      ),
    ).toBe("Attach a receipt that covers this entry.");
  });
});
