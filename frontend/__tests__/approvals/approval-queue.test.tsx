import { describe, it, expect, vi, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  within,
} from "@testing-library/react";

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

const HIGH = item({
  id: "high",
  subject_title: "Q3 Conference",
  risk_level: "high",
});
const LOW = item({
  id: "low",
  subject_title: "Team Lunch",
  submitter_name: "Carlos Rivera",
  risk_level: "low",
});
const AUTO = item({
  id: "auto",
  subject_title: "Office Supplies",
  current_status: "auto_approved",
  is_auto_approved: true,
  risk_level: "low",
  auto_approve_rationale: "Auto-approved: in policy, under threshold.",
});

function renderQueue(
  props: Partial<React.ComponentProps<typeof ApprovalQueue>> = {},
) {
  return render(
    <ApprovalQueue
      items={[HIGH, LOW, AUTO]}
      isLoading={false}
      isError={false}
      onRetry={vi.fn()}
      selectedId={null}
      viewedIds={new Set()}
      onSelect={vi.fn()}
      {...props}
    />,
  );
}

describe("ApprovalQueue", () => {
  it("renders active (non-auto) rows with risk + status badges", () => {
    renderQueue();
    expect(screen.getByText("Q3 Conference")).toBeTruthy();
    expect(screen.getByText("Team Lunch")).toBeTruthy();
    expect(screen.getByText("High risk")).toBeTruthy();
    // status badge for pending
    expect(screen.getAllByText("Pending").length).toBeGreaterThan(0);
  });

  it("keeps auto-approved items out of the active queue (in the collapsed lane)", () => {
    renderQueue();
    // collapsed lane → rationale + title not yet visible
    expect(screen.queryByText("Office Supplies")).toBeNull();
    expect(screen.getByText("Auto-Approved (1)")).toBeTruthy();
  });

  it("expands the auto-approved lane to reveal each item's rationale (G8)", () => {
    renderQueue();
    fireEvent.click(screen.getByText("Auto-Approved (1)"));
    expect(screen.getByText("Office Supplies")).toBeTruthy();
    expect(
      screen.getByText("Auto-approved: in policy, under threshold."),
    ).toBeTruthy();
  });

  it("risk filter narrows the active queue (display-only)", () => {
    renderQueue();
    fireEvent.click(screen.getByRole("button", { name: "high" }));
    expect(screen.getByText("Q3 Conference")).toBeTruthy();
    expect(screen.queryByText("Team Lunch")).toBeNull();
  });

  it("search filters by submitter or subject (display-only)", () => {
    renderQueue();
    fireEvent.change(screen.getByLabelText("Search approvals"), {
      target: { value: "carlos" },
    });
    expect(screen.getByText("Team Lunch")).toBeTruthy();
    expect(screen.queryByText("Q3 Conference")).toBeNull();
  });

  it("shows an unread dot for pending items not yet viewed", () => {
    const { rerender } = renderQueue({ viewedIds: new Set() });
    expect(screen.getAllByLabelText("Unread").length).toBe(2);
    rerender(
      <ApprovalQueue
        items={[HIGH, LOW, AUTO]}
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
        selectedId={null}
        viewedIds={new Set(["high"])}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getAllByLabelText("Unread").length).toBe(1);
  });

  it("calls onSelect with the row id when clicked", () => {
    const onSelect = vi.fn();
    renderQueue({ onSelect });
    fireEvent.click(screen.getByText("Q3 Conference"));
    expect(onSelect).toHaveBeenCalledWith("high");
  });

  it("renders the empty state when there are no active items", () => {
    render(
      <ApprovalQueue
        items={[]}
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
        selectedId={null}
        viewedIds={new Set()}
        onSelect={vi.fn()}
      />,
    );
    // C7-25: lane-appropriate copy replaced the misleading "No pending approvals".
    expect(screen.getByText("Approval queue is empty")).toBeTruthy();
    expect(screen.queryByText("No pending approvals")).toBeNull();
  });

  it("renders an error state with retry", () => {
    const onRetry = vi.fn();
    render(
      <ApprovalQueue
        items={[]}
        isLoading={false}
        isError
        onRetry={onRetry}
        selectedId={null}
        viewedIds={new Set()}
        onSelect={vi.fn()}
      />,
    );
    const retry = screen.getByText("Retry");
    fireEvent.click(retry);
    expect(onRetry).toHaveBeenCalled();
  });

  it("marks the selected row with aria-current", () => {
    renderQueue({ selectedId: "high" });
    const selected = screen
      .getByText("Q3 Conference")
      .closest("button") as HTMLButtonElement;
    expect(selected.getAttribute("aria-current")).toBe("true");
    // a non-selected row is not current
    const other = screen
      .getByText("Team Lunch")
      .closest("button") as HTMLButtonElement;
    expect(within(other).queryByLabelText("Unread")).toBeTruthy();
  });
});
