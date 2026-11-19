/**
 * Product gap 2 review fix — the approver must SEE what a human check asks it to
 * confirm: the stored mileage trip (MIL-01/02/03), the attendee count
 * (MEAL-05 / ENT-02) and the gift disclosure (ENT-04), in the decision panel.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));

import { DecisionPanel } from "@/components/approvals/decision-panel";
import type { ApprovalDetail } from "@/lib/types/approvals";
import type { ExpenseEntry } from "@/lib/types/expenses";

afterEach(cleanup);

function entry(over: Partial<ExpenseEntry>): ExpenseEntry {
  return {
    id: "e-x",
    organization_id: "org",
    report_id: "r1",
    trip_segment_id: null,
    category: "meals",
    amount: "40.00",
    currency: "USD",
    expense_date: "2026-06-11",
    vendor: null,
    description: null,
    status: "draft",
    match_source: "manual",
    is_itemized: false,
    created_by: "u",
    updated_by: "u",
    created_at: "2026-06-11T10:00:00Z",
    updated_at: "2026-06-11T10:00:00Z",
    receipt_id: null,
    ...over,
  };
}

const DRIVE = entry({
  id: "e-drive",
  category: "mileage",
  amount: "287.00",
  // The claimant replaced the generated route text — the trip must still show.
  description: "hidden",
  mileage: {
    distance: "215",
    unit: "mi",
    origin: "Boston office",
    destination: "New York",
    round_trip: true,
    commute_offset: "20",
    air_impractical: false,
    one_way_miles: "215.0",
    claimed_miles: "430.0",
    commute_offset_miles: "20.0",
    billable_miles: "410.0",
    rate_per_mile: "0.70",
    rate_clause: "MIL-01",
  },
});
const TEAM_MEAL = entry({
  id: "e-meal",
  vendor: "Trattoria",
  amount: "180.00",
  attendee_count: 4,
});
const GIFT = entry({
  id: "e-gift",
  category: "gift",
  vendor: "Gift shop",
  amount: "250.00",
  disclosure: {
    recipient: "Jane Roe",
    recipient_organization: "Port Authority",
    business_purpose: "Customs facility tour",
    government_official: true,
  },
});

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
    version: 1,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    steps: [],
    expense_report: {
      report: {
        id: "r1",
        organization_id: "org",
        owner_id: "u-sub",
        trip_id: null,
        title: "June",
        status: "submitted",
        total_amount: "717.00",
        currency: "USD",
        submitted_at: "2026-06-12T00:00:00Z",
        pre_submit_flags: null,
        created_by: null,
        updated_by: null,
        created_at: "2026-06-12T00:00:00Z",
        updated_at: "2026-06-12T00:00:00Z",
        version: 1,
      },
      entries,
    },
    trip: null,
  };
}

function renderPanel(d: ApprovalDetail) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <DecisionPanel
        approvalId={d.id}
        detail={d}
        isLoading={false}
        isError={false}
        onRetry={() => {}}
        onBack={() => {}}
        canDecide
        currentUserId="u-mgr"
      />
    </QueryClientProvider>,
  );
}

describe("approver sees the facts behind product-gap-2 human checks", () => {
  it("shows the stored mileage trip even when the description hides it", () => {
    renderPanel(detail([DRIVE]));
    const box = within(screen.getByTestId("entry-policy-details-e-drive"));
    expect(box.getByTestId("entry-policy-trip").textContent).toBe(
      "Boston office → New York",
    );
    expect(box.getByTestId("entry-policy-distance").textContent).toBe(
      "215 mi one way, round trip · commute offset 20 mi",
    );
    expect(box.getByTestId("entry-policy-billable").textContent).toBe(
      "410.0 mi × $0.70/mi (MIL-01)",
    );
  });

  it("shows the attendee count and the full gift disclosure", () => {
    renderPanel(detail([TEAM_MEAL, GIFT]));
    const meal = within(screen.getByTestId("entry-policy-details-e-meal"));
    expect(meal.getByTestId("entry-policy-attendees").textContent).toBe(
      "4 (claimant included)",
    );
    const gift = within(screen.getByTestId("entry-policy-details-e-gift"));
    expect(gift.getByTestId("entry-policy-recipient").textContent).toBe(
      "Jane Roe (Port Authority)",
    );
    expect(gift.getByTestId("entry-policy-purpose").textContent).toBe(
      "Customs facility tour",
    );
    expect(gift.getByTestId("entry-policy-official").textContent).toBe("Yes");
  });

  it("says a mileage line's trip is missing instead of showing nothing", () => {
    renderPanel(detail([{ ...DRIVE, mileage: null }]));
    expect(
      screen.getByTestId("entry-policy-details-e-drive").textContent,
    ).toContain("Trip details missing");
  });

  it("renders nothing extra for a plain line", () => {
    renderPanel(detail([entry({ id: "e-plain", vendor: "Cafe" })]));
    expect(screen.queryByTestId("entry-policy-details-e-plain")).toBeNull();
  });
});
