/**
 * Known limits (ground) — the entry-time hint for a ground-transport line whose
 * kind nothing trusted confirms (typed-only rides).
 *
 *  - The readiness normalizer reads the server's `observed.confirm_ground_type`.
 *  - The suggested fix asks for a receipt or card match that names the ride.
 *  - The entry editor shows the hint for such a line, and only for such a line.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

vi.mock("@/lib/query/expense-hooks", () => ({
  useUpdateEntry: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import { EntryEditor } from "@/components/expenses/entry-editor";
import {
  CONFIRM_GROUND_TYPE_HINT,
  normalizePreSubmitFlags,
  suggestedFix,
  type ReadinessFlag,
} from "@/lib/expenses/readiness";
import type { ExpenseEntry } from "@/lib/types";

afterEach(cleanup);

const ENTRY: ExpenseEntry = {
  id: "e1",
  organization_id: "o1",
  report_id: "r1",
  trip_segment_id: null,
  category: "ground_transport",
  amount: "14.00",
  currency: "USD",
  expense_date: "2026-11-10",
  vendor: "Uber",
  description: "Ride to client",
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

const SERVER_FLAGS = {
  flags: [
    {
      flag_type: "policy_advisory",
      severity: "advisory",
      source_clause_ref: "GRND-01",
      reason: "Ground transport, category unverified: … within every limit.",
      entry_id: "e1",
      observed: { confirm_ground_type: true, ground_worst_case_cleared: true },
    },
    {
      flag_type: "policy_advisory",
      severity: "advisory",
      source_clause_ref: "LOD-02",
      reason: "Within the LOD-02 tolerance.",
      entry_id: "e2",
      observed: { confirm_ground_type: "yes" },
    },
  ],
};

function editor(flags: ReadinessFlag[]) {
  return render(
    <EntryEditor
      entry={ENTRY}
      report={{ id: "r1", version: 1 }}
      flags={flags}
      onClose={() => {}}
    />,
  );
}

describe("ground type hint", () => {
  it("reads confirm_ground_type only when the server sets it to true", () => {
    const summary = normalizePreSubmitFlags(SERVER_FLAGS);
    expect(summary.flags.map((f) => f.confirmGroundType)).toEqual([
      true,
      undefined,
    ]);
  });

  it("suggests adding a receipt or card match to confirm the type", () => {
    const [ground, lodging] = normalizePreSubmitFlags(SERVER_FLAGS).flags;
    expect(suggestedFix(ground)).toBe(CONFIRM_GROUND_TYPE_HINT);
    expect(suggestedFix(lodging)).not.toBe(CONFIRM_GROUND_TYPE_HINT);
  });

  it("shows the hint in the entry editor for an unconfirmed ground line", () => {
    const [ground] = normalizePreSubmitFlags(SERVER_FLAGS).flags;
    editor([ground]);
    expect(screen.getByTestId("entry-ground-type-hint").textContent).toBe(
      CONFIRM_GROUND_TYPE_HINT,
    );
  });

  it("shows no hint when no flag asks for it", () => {
    const [, lodging] = normalizePreSubmitFlags(SERVER_FLAGS).flags;
    editor([lodging]);
    expect(screen.queryByTestId("entry-ground-type-hint")).toBeNull();
  });
});
