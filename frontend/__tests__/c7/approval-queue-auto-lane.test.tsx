import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import { ApprovalQueue } from "@/components/approvals/approval-queue";
import type { ApprovalListItem } from "@/lib/types/approvals";

afterEach(cleanup);

function item(over: Partial<ApprovalListItem>): ApprovalListItem {
  return {
    id: "a1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "s1",
    submitter_id: "u1",
    submitter_name: "Alex Employee",
    subject_title: "Q3 Conference",
    subject_amount: "1234.50",
    currency: "USD",
    current_status: "pending",
    is_auto_approved: false,
    risk_level: "high",
    auto_approve_rationale: null,
    created_at: "2026-06-12T00:00:00Z",
    version: 1,
    ...over,
  };
}

const PENDING = item({ id: "p1", subject_title: "Q3 Conference" });
const AUTO_SUPPLIES = item({
  id: "auto1",
  subject_title: "Office Supplies",
  submitter_name: "Alex Employee",
  current_status: "auto_approved",
  is_auto_approved: true,
  risk_level: "low",
});
const AUTO_TAXI = item({
  id: "auto2",
  subject_title: "Airport Taxi",
  submitter_name: "Carlos Rivera",
  current_status: "auto_approved",
  is_auto_approved: true,
  risk_level: "low",
});

function renderQueue(items: ApprovalListItem[]) {
  return render(
    <ApprovalQueue
      items={items}
      isLoading={false}
      isError={false}
      onRetry={vi.fn()}
      selectedId={null}
      viewedIds={new Set()}
      onSelect={vi.fn()}
    />,
  );
}

describe("C7-25 Auto-Approved lane honours search + risk filter", () => {
  it("search narrows the auto-approved lane", () => {
    renderQueue([PENDING, AUTO_SUPPLIES, AUTO_TAXI]);
    fireEvent.change(screen.getByLabelText("Search approvals"), {
      target: { value: "carlos" },
    });
    // Count reflects the filter (was always the unfiltered "(2)").
    fireEvent.click(screen.getByText("Auto-Approved (1 of 2)"));
    expect(screen.getByText("Airport Taxi")).toBeTruthy();
    expect(screen.queryByText("Office Supplies")).toBeNull();
  });

  it("risk filter narrows the auto-approved lane", () => {
    renderQueue([PENDING, AUTO_SUPPLIES, AUTO_TAXI]);
    fireEvent.click(screen.getByRole("button", { name: "high" }));
    fireEvent.click(screen.getByText("Auto-Approved (0 of 2)"));
    expect(screen.queryByText("Office Supplies")).toBeNull();
    expect(screen.queryByText("Airport Taxi")).toBeNull();
    expect(
      screen.getByText("No auto-approved items match your search or filter."),
    ).toBeTruthy();
  });

  it("no filter → unchanged count and rows", () => {
    renderQueue([PENDING, AUTO_SUPPLIES, AUTO_TAXI]);
    fireEvent.click(screen.getByText("Auto-Approved (2)"));
    expect(screen.getByText("Office Supplies")).toBeTruthy();
    expect(screen.getByText("Airport Taxi")).toBeTruthy();
  });
});

describe("C7-25 lane-appropriate empty copy", () => {
  it("only auto-approved items: queue lane does not claim 'No pending approvals'", () => {
    renderQueue([AUTO_SUPPLIES]);
    expect(screen.queryByText("No pending approvals")).toBeNull();
    expect(screen.getByText("Approval queue is empty")).toBeTruthy();
  });

  it("filter hides every active item: says no matches, not 'No pending approvals'", () => {
    renderQueue([PENDING, AUTO_SUPPLIES]);
    fireEvent.change(screen.getByLabelText("Search approvals"), {
      target: { value: "zzz" },
    });
    expect(screen.queryByText("No pending approvals")).toBeNull();
    expect(screen.getByText("No matching approvals")).toBeTruthy();
  });

  it("decided-only queue: copy does not say pending", () => {
    renderQueue([
      item({ id: "d1", current_status: "approved", subject_title: "Done" }),
    ]);
    fireEvent.click(screen.getByRole("button", { name: "low" }));
    expect(screen.queryByText("No pending approvals")).toBeNull();
  });
});
