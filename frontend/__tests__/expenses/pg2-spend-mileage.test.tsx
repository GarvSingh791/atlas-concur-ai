/**
 * Product gap 2 — attendees, gift disclosure and mileage in the entry editor.
 *
 *  - attendees show only for meals / client entertainment, the disclosure only
 *    for gifts / client entertainment, the trip only for mileage entries;
 *  - a mileage entry's amount is read-only and NEVER sent (the server computes
 *    it); a trip edit sends the trip; no other entry can become mileage;
 *  - "Add mileage" posts the trip without an amount;
 *  - the display-only estimate rounds like the server (half up, to the cent).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

const h = vi.hoisted(() => ({
  updateMutate: vi.fn(),
  addMutate: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/lib/query/expense-hooks", () => ({
  useUpdateEntry: () => ({ mutate: h.updateMutate, isPending: false }),
}));
vi.mock("@/lib/query/mileage-hooks", () => ({
  useAddMileage: () => ({ mutate: h.addMutate, isPending: false }),
  // Follow-up: the dialog also reads the rate for its live estimate.
  useMileageRate: () => ({ data: undefined }),
}));
vi.mock("@/lib/query/receipt-hooks", () => ({
  useDeleteReceipt: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: h.showToast }),
}));

import {
  EntryEditor,
  diffEntryChanges,
} from "@/components/expenses/entry-editor";
import { AddMileageDialog } from "@/components/expenses/add-mileage-dialog";
import {
  disclosureOf,
  estimateMileageAmount,
  mileageFormError,
  parseAttendees,
  EMPTY_MILEAGE_FORM,
} from "@/lib/expenses/policy-fields";
import { suggestedFix, type ReadinessFlag } from "@/lib/expenses/readiness";
import type { ExpenseEntry } from "@/lib/types";

const TRIP = {
  distance: "120.5",
  unit: "mi" as const,
  origin: "Boston office",
  destination: "Providence",
  round_trip: true,
  commute_offset: "20",
  air_impractical: false,
  one_way_miles: "120.5",
  claimed_miles: "241.0",
  commute_offset_miles: "20.0",
  billable_miles: "221.0",
  rate_per_mile: "0.70",
  rate_clause: "MIL-01",
};

function entry(over: Partial<ExpenseEntry> = {}): ExpenseEntry {
  return {
    id: "e-1",
    organization_id: "org",
    report_id: "rep-1",
    trip_segment_id: null,
    category: "meals",
    amount: "200.00",
    currency: "USD",
    expense_date: "2026-06-10",
    vendor: "Bistro",
    description: null,
    status: "draft",
    match_source: null,
    is_itemized: false,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    justification: null,
    attendee_count: null,
    disclosure: null,
    mileage: null,
    ...over,
  };
}

const MILEAGE = entry({
  id: "e-mi",
  category: "mileage",
  amount: "154.70",
  vendor: null,
  mileage: TRIP,
});

function mount(e: ExpenseEntry) {
  return render(
    <EntryEditor
      entry={e}
      report={{ id: "rep-1", version: 3 }}
      onClose={() => {}}
    />,
  );
}

function categoryOptions(): string[] {
  const select = screen.getByLabelText("Category") as HTMLSelectElement;
  return Array.from(select.options).map((o) => o.value);
}

beforeEach(() => {
  h.updateMutate.mockReset();
  h.addMutate.mockReset();
});
afterEach(cleanup);

describe("category-gated fields", () => {
  it("meals: attendees only", () => {
    mount(entry());
    expect(screen.getByTestId("entry-attendees")).toBeTruthy();
    expect(screen.queryByTestId("entry-disclosure")).toBeNull();
    expect(screen.queryByTestId("entry-mileage")).toBeNull();
    expect(categoryOptions()).not.toContain("mileage");
  });

  it("gift: disclosure only; entertainment: both; lodging: neither", () => {
    mount(entry({ category: "gift" }));
    expect(screen.getByTestId("entry-disclosure")).toBeTruthy();
    expect(screen.queryByTestId("entry-attendees")).toBeNull();
    cleanup();
    mount(entry({ category: "entertainment" }));
    expect(screen.getByTestId("entry-disclosure")).toBeTruthy();
    expect(screen.getByTestId("entry-attendees")).toBeTruthy();
    cleanup();
    mount(entry({ category: "lodging" }));
    expect(screen.queryByTestId("entry-disclosure")).toBeNull();
    expect(screen.queryByTestId("entry-attendees")).toBeNull();
  });

  it("sends the attendee count and the disclosure", () => {
    mount(entry({ category: "entertainment", amount: "450.00" }));
    fireEvent.change(screen.getByTestId("entry-attendees"), {
      target: { value: "3" },
    });
    fireEvent.change(screen.getByLabelText("Recipient"), {
      target: { value: "Pat Client" },
    });
    fireEvent.change(screen.getByLabelText("Business purpose"), {
      target: { value: "Renewal" },
    });
    fireEvent.change(
      screen.getByLabelText("Is the recipient a government official?"),
      { target: { value: "no" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(h.updateMutate).toHaveBeenCalledTimes(1);
    const changes = h.updateMutate.mock.calls[0][0].changes;
    expect(changes).toEqual({
      attendee_count: 3,
      disclosure: {
        recipient: "Pat Client",
        recipient_organization: null,
        business_purpose: "Renewal",
        government_official: false,
      },
    });
  });

  it("refuses a non-numeric attendee count", () => {
    mount(entry());
    fireEvent.change(screen.getByTestId("entry-attendees"), {
      target: { value: "two" },
    });
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(h.updateMutate).not.toHaveBeenCalled();
    expect(screen.getByRole("alert").textContent).toMatch(/attendees/);
  });
});

describe("mileage entry", () => {
  it("shows the server figures, a read-only amount and a locked category", () => {
    mount(MILEAGE);
    const computed = screen.getByTestId("mileage-computed").textContent ?? "";
    expect(computed).toContain("221.0 business mi");
    expect(computed).toContain("$0.70/mi");
    expect(computed).toContain("MIL-01");
    const amount = screen.getByLabelText("Amount (USD)") as HTMLInputElement;
    expect(amount.readOnly).toBe(true);
    const category = screen.getByLabelText("Category") as HTMLSelectElement;
    expect(category.disabled).toBe(true);
  });

  it("sends the trip, never an amount", () => {
    mount(MILEAGE);
    fireEvent.change(screen.getByLabelText("Amount (USD)"), {
      target: { value: "999.00" },
    });
    fireEvent.change(screen.getByTestId("mileage-distance"), {
      target: { value: "130" },
    });
    expect(screen.getByTestId("mileage-computed").textContent).toContain(
      "about $168.00",
    );
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    const changes = h.updateMutate.mock.calls[0][0].changes;
    expect(changes).not.toHaveProperty("amount");
    expect(changes.mileage).toEqual({
      distance: "130",
      unit: "mi",
      origin: "Boston office",
      destination: "Providence",
      round_trip: true,
      commute_offset: "20",
      air_impractical: false,
    });
  });

  it("re-sends an unchanged trip when the line is blocked by a mileage rule (rate change)", () => {
    // Finance raised the MIL-01 rate: the stored amount no longer equals
    // billable miles x the current rate and the line is blocked. Nothing in the
    // trip changed, yet Save must send it so the server recomputes the amount.
    const stale: ReadinessFlag = {
      severity: "issue",
      message:
        "Mileage amount $154.70 does not equal 221.0 business mi x $0.725/mi = $160.23 (MIL-01).",
      clauseRef: "MIL-01",
      entryRef: null,
      flagType: "policy_violation",
      entryId: "e-mi",
      ruleType: "mileage",
    };
    render(
      <EntryEditor
        entry={MILEAGE}
        report={{ id: "rep-1", version: 3 }}
        flags={[stale]}
        onClose={() => {}}
      />,
    );
    expect(screen.getByTestId("mileage-recompute-hint").textContent).toMatch(
      /recompute/i,
    );
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(h.updateMutate).toHaveBeenCalledTimes(1);
    const changes = h.updateMutate.mock.calls[0][0].changes;
    expect(changes).not.toHaveProperty("amount");
    expect(changes.mileage).toEqual({
      distance: "120.5",
      unit: "mi",
      origin: "Boston office",
      destination: "Providence",
      round_trip: true,
      commute_offset: "20",
      air_impractical: false,
    });
  });

  it("an unchanged, unflagged mileage line saves nothing (no request)", () => {
    const onClose = vi.fn();
    const advisory: ReadinessFlag = {
      severity: "review",
      message: "Atlas cannot verify the route distance (MIL-01).",
      clauseRef: "MIL-01",
      entryRef: null,
      flagType: "approval_required",
      entryId: "e-mi",
      ruleType: "mileage",
    };
    render(
      <EntryEditor
        entry={MILEAGE}
        report={{ id: "rep-1", version: 3 }}
        flags={[advisory]}
        onClose={onClose}
      />,
    );
    expect(screen.queryByTestId("mileage-recompute-hint")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /save changes/i }));
    expect(h.updateMutate).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it("diffEntryChanges never emits an amount for mileage", () => {
    const changes = diffEntryChanges(MILEAGE, {
      amount: "1.00",
      vendor: "",
      category: "mileage",
      expenseDate: "2026-06-10",
      description: "",
      justification: "",
      affidavit: false, // EXP-04 field of the merged form (integration)
    });
    expect(changes).toEqual({});
  });
});

describe("Add mileage dialog", () => {
  it("posts the trip without an amount", () => {
    render(
      <AddMileageDialog
        report={{ id: "rep-1", version: 4 }}
        onOpenChange={() => {}}
      />,
    );
    fireEvent.change(screen.getByLabelText("Date"), {
      target: { value: "2026-06-10" },
    });
    fireEvent.change(screen.getByLabelText("From"), {
      target: { value: "HQ" },
    });
    fireEvent.change(screen.getByLabelText("To"), {
      target: { value: "Client" },
    });
    fireEvent.change(screen.getByTestId("mileage-distance"), {
      target: { value: "42.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add mileage" }));
    expect(h.addMutate).toHaveBeenCalledTimes(1);
    const args = h.addMutate.mock.calls[0][0];
    expect(args.reportVersion).toBe(4);
    expect(args.body).toEqual({
      expense_date: "2026-06-10",
      distance: "42.5",
      unit: "mi",
      origin: "HQ",
      destination: "Client",
      round_trip: false,
      commute_offset: "0",
      air_impractical: false,
    });
    expect(args.body).not.toHaveProperty("amount");
  });

  it("asks for a date and a valid distance first", () => {
    render(
      <AddMileageDialog
        report={{ id: "rep-1", version: 4 }}
        onOpenChange={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Add mileage" }));
    expect(screen.getByRole("alert").textContent).toMatch(/date/);
    expect(h.addMutate).not.toHaveBeenCalled();
  });
});

describe("policy-field helpers", () => {
  it("estimates like the server (Decimal, half up)", () => {
    const t = { distance: "10", unit: "mi" as const, round_trip: false };
    expect(estimateMileageAmount({ ...t, commute_offset: "0" }, "0.70")).toBe(
      "7.00",
    );
    expect(
      estimateMileageAmount(
        {
          distance: "120.5",
          unit: "mi",
          round_trip: true,
          commute_offset: "20",
        },
        "0.70",
      ),
    ).toBe("154.70");
    expect(
      estimateMileageAmount(
        { distance: "0.2", unit: "mi", round_trip: false, commute_offset: "0" },
        "0.725",
      ),
    ).toBe("0.15");
    expect(
      estimateMileageAmount(
        { distance: "100", unit: "km", round_trip: false, commute_offset: "0" },
        "0.70",
      ),
    ).toBe("43.47"); // 62.1 mi
    expect(
      estimateMileageAmount({ ...t, commute_offset: "10" }, "0.70"),
    ).toBeNull();
    expect(estimateMileageAmount({ ...t, commute_offset: "0" }, "abc")).toBe(
      null,
    );
  });

  it("validates the trip and the attendees", () => {
    expect(mileageFormError({ ...EMPTY_MILEAGE_FORM })).toMatch(/distance/);
    expect(
      mileageFormError({
        ...EMPTY_MILEAGE_FORM,
        distance: "10.25",
        origin: "a",
        destination: "b",
      }),
    ).toMatch(/distance/);
    expect(
      mileageFormError({
        ...EMPTY_MILEAGE_FORM,
        distance: "10",
        origin: "a",
        destination: "b",
      }),
    ).toBeNull();
    expect(parseAttendees("")).toBeNull();
    expect(parseAttendees("4")).toBe(4);
    expect(parseAttendees("0")).toBeUndefined();
    expect(parseAttendees("501")).toBeUndefined();
  });

  it("an all-blank disclosure is no disclosure", () => {
    expect(
      disclosureOf({
        recipient: " ",
        recipientOrganization: "",
        businessPurpose: "",
        governmentOfficial: "",
      }),
    ).toBeNull();
  });

  it("suggests the data fix for a disclosure / mileage block", () => {
    const flag = (ruleType: string): ReadinessFlag => ({
      severity: "issue",
      message: "m",
      clauseRef: null,
      entryRef: null,
      flagType: "policy_violation",
      ruleType,
      justifiable: false,
    });
    expect(suggestedFix(flag("gift_disclosure"))).toMatch(/gift disclosure/);
    expect(suggestedFix(flag("mileage"))).toMatch(/recomputes/);
  });
});
