/**
 * QA round — finance /approvals detail pane.
 *
 *  - A withdrawn report shows "Withdrawn" in the detail header (the server's
 *    display_status, like the queue) and the Decision section says the report
 *    was withdrawn — not "This approval is changes requested and can't be
 *    changed". Closed approvals read "This approval was <outcome> …".
 *  - A flag whose entry_ref is an internal key ("entry_0", a mileage line has
 *    no vendor) is titled with a readable name — never the raw key.
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
import type { ApprovalDetail, RiskFlag } from "@/lib/types/approvals";
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

const MILEAGE = entry({
  id: "e-miles",
  category: "mileage",
  amount: "33.50",
  created_at: "2026-06-11T09:00:00Z",
});
const LUNCH = entry({ id: "e-lunch", vendor: "Corner Cafe" });

function mileageFlag(rule: string, over: Partial<RiskFlag> = {}): RiskFlag {
  return {
    entry_ref: "entry_0",
    flag_type: "approval_required",
    rule_id: rule,
    source_clause_ref: rule,
    reason: `${rule}: confirm the mileage claim.`,
    severity: "advisory",
    ...over,
  };
}

function detail(
  over: Partial<ApprovalDetail>,
  flags: RiskFlag[] = [],
  entries: ExpenseEntry[] = [MILEAGE, LUNCH],
): ApprovalDetail {
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
      flags,
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
        title: "Client visits",
        status: "submitted",
        total_amount: "73.50",
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
    ...over,
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
        currentUserId="u-fin"
      />
    </QueryClientProvider>,
  );
}

describe("withdrawn report in the detail pane", () => {
  it("header badge and decision text say withdrawn, not changes requested", () => {
    const { container } = renderPanel(
      detail({
        current_status: "changes_requested",
        subject_withdrawn: true,
        display_status: "withdrawn",
      }),
    );
    const header = container.querySelector("header") as HTMLElement;
    expect(within(header).getByText("Withdrawn")).toBeTruthy();
    expect(within(header).queryByText("Changes requested")).toBeNull();
    expect(
      screen.getByText(
        "This report was withdrawn by its owner; the approval is closed.",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/changes requested and can/)).toBeNull();
  });

  it.each([
    ["approved", "This approval was approved and can’t be changed."],
    ["rejected", "This approval was rejected and can’t be changed."],
    [
      "changes_requested",
      "This approval was returned for changes and can’t be changed.",
    ],
  ] as const)("closed %s approval reads grammatically", (status, text) => {
    const { container } = renderPanel(
      detail({ current_status: status, display_status: status }),
    );
    expect(screen.getByText(text)).toBeTruthy();
    expect(container.textContent).not.toMatch(/approval is \w+ ?\w* and/);
  });
});

describe("mileage flags are titled readably", () => {
  it("never renders the raw entry_<n> key; uses the entry's name", () => {
    const { container } = renderPanel(
      detail({}, [mileageFlag("MIL-01"), mileageFlag("MIL-02")]),
    );
    expect(container.textContent).not.toMatch(/entry_\d/);
    expect(screen.getAllByText("Mileage entry").length).toBeGreaterThanOrEqual(
      2,
    );
  });

  it("prefers the entry description, resolved by entry_id", () => {
    const described = { ...MILEAGE, description: "Drive to Acme HQ" };
    const { container } = renderPanel(
      detail(
        {},
        [mileageFlag("MIL-01", { entry_ref: "entry_5", entry_id: "e-miles" })],
        [described, LUNCH],
      ),
    );
    expect(container.textContent).not.toMatch(/entry_\d/);
    expect(screen.getAllByText("Drive to Acme HQ").length).toBeGreaterThan(0);
  });

  it("an unresolvable internal key falls back to a generic name", () => {
    const { container } = renderPanel(
      detail({}, [mileageFlag("MIL-01", { entry_ref: "entry_9" })]),
    );
    expect(container.textContent).not.toMatch(/entry_\d/);
    expect(screen.getByText("Expense entry")).toBeTruthy();
  });
});
