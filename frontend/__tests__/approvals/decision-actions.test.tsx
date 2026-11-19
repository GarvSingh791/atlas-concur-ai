import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";

import { DecisionActions } from "@/components/approvals/decision-actions";
import { ApiError } from "@/lib/types/api";
import type { ApprovalDetail } from "@/lib/types/approvals";

// Mock the toast + decide mutation so we can assert behavior in isolation.
const { showToast, mutateAsync } = vi.hoisted(() => ({
  showToast: vi.fn(),
  mutateAsync: vi.fn(),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/query/approval-hooks", () => ({
  useDecideApproval: () => ({ mutateAsync, isPending: false }),
}));

afterEach(cleanup);
beforeEach(() => {
  showToast.mockReset();
  mutateAsync.mockReset();
  mutateAsync.mockResolvedValue({});
});

function detail(over: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u1",
    submitter_name: "Alex",
    current_status: "pending",
    is_auto_approved: false,
    risk_summary: null,
    risk_level: "medium",
    version: 4,
    created_at: "2026-06-12T00:00:00Z",
    updated_at: "2026-06-12T00:00:00Z",
    steps: [],
    expense_report: null,
    trip: null,
    ...over,
  };
}

describe("DecisionActions", () => {
  it("hides actions when the user cannot decide", () => {
    render(<DecisionActions approval={detail()} canDecide={false} />);
    expect(screen.getByText(/don’t have permission to decide/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });

  it("disables actions on an already-resolved approval", () => {
    render(
      <DecisionActions
        approval={detail({ current_status: "approved" })}
        canDecide
      />,
    );
    expect(screen.getByText(/can’t be\s+changed/i)).toBeTruthy();
  });

  it("approves immediately with the current version (If-Match)", async () => {
    render(<DecisionActions approval={detail()} canDecide />);
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      id: "ap-1",
      decision: "approve",
      version: 4,
    });
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "success", title: "Approved" }),
      ),
    );
  });

  it("requires a reason before a reject can be submitted", async () => {
    render(<DecisionActions approval={detail()} canDecide />);
    // Only the action button exists before the modal opens.
    fireEvent.click(screen.getByRole("button", { name: /Reject/ }));
    await screen.findByRole("dialog");

    // After opening, the dialog's confirm "Reject" is appended last (portal).
    const confirm = () =>
      screen
        .getAllByRole("button", { name: "Reject" })
        .at(-1) as HTMLButtonElement;
    expect(confirm().disabled).toBe(true);
    expect(mutateAsync).not.toHaveBeenCalled();

    fireEvent.change(screen.getByPlaceholderText("Enter your reason..."), {
      target: { value: "Lodging exceeds policy" },
    });
    expect(confirm().disabled).toBe(false);

    fireEvent.click(confirm());
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      decision: "reject",
      reason: "Lodging exceeds policy",
      version: 4,
    });
  });

  it("submits request_changes with a reason", async () => {
    render(<DecisionActions approval={detail()} canDecide />);
    fireEvent.click(screen.getByRole("button", { name: /Request changes/ }));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByPlaceholderText("Enter your reason..."), {
      target: { value: "Need itemized receipts" },
    });
    fireEvent.click(
      screen.getAllByRole("button", { name: "Request changes" }).at(-1)!,
    );
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledTimes(1));
    expect(mutateAsync.mock.calls[0][0]).toMatchObject({
      decision: "request_changes",
      reason: "Need itemized receipts",
    });
  });

  it("surfaces a 409 version conflict as an error toast (no silent overwrite)", async () => {
    mutateAsync.mockRejectedValue(
      new ApiError(
        {
          code: "CONFLICT",
          message: "Record was modified concurrently; reload and retry.",
          correlation_id: "abc",
        },
        409,
      ),
    );
    render(<DecisionActions approval={detail()} canDecide />);
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "error",
          title: "This approval changed",
        }),
      ),
    );
  });

  it("notes a reverse-within-policy override on auto-approved items", () => {
    render(
      <DecisionActions
        approval={detail({
          current_status: "auto_approved",
          is_auto_approved: true,
        })}
        canDecide
      />,
    );
    expect(screen.getByText(/records a human override/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Approve/ })).toBeTruthy();
  });
});
