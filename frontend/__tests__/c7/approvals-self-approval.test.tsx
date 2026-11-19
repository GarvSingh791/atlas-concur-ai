/**
 * C7-01/02/03 + C7-13 follow-up (approvals UI).
 *
 * - PATCH /approvals/{id} returns 403 when the caller is the submitter: the UI
 *   hides Approve / Reject / Request changes on one's own submission and says
 *   why.
 * - 403 FORBIDDEN and 400 INVALID_SUBJECT_STATE surface the envelope message
 *   with a specific title, not the generic "Decision failed".
 * - An INVALID_SUBJECT_STATE (subject moved on) refetches the stale detail.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
  renderHook,
  act,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { showToast, patch } = vi.hoisted(() => ({
  showToast: vi.fn(),
  patch: vi.fn(),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get: vi.fn(), post: vi.fn(), patch },
}));

import { DecisionActions } from "@/components/approvals/decision-actions";
import { DecisionPanel } from "@/components/approvals/decision-panel";
import { approvalKeys, useDecideApproval } from "@/lib/query/approval-hooks";
import { API_ERROR_CODES, ApiError } from "@/lib/types/api";
import type { ApprovalDetail } from "@/lib/types/approvals";

function detail(over: Partial<ApprovalDetail> = {}): ApprovalDetail {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u-sub",
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

function withClient(ui: React.ReactElement, client?: QueryClient) {
  const qc =
    client ??
    new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function apiError(code: string, message: string, status: number) {
  return new ApiError({ code, message, correlation_id: "cid" }, status);
}

afterEach(cleanup);
beforeEach(() => {
  showToast.mockReset();
  patch.mockReset();
});

describe("error-code catalogue", () => {
  it("mirrors the backend ErrorCode values and the approval state code", () => {
    expect(API_ERROR_CODES).toMatchObject({
      METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
      NOT_ACCEPTABLE: "NOT_ACCEPTABLE",
      PAYLOAD_TOO_LARGE: "PAYLOAD_TOO_LARGE",
      UNSUPPORTED_MEDIA_TYPE: "UNSUPPORTED_MEDIA_TYPE",
      TOO_MANY_REQUESTS: "TOO_MANY_REQUESTS",
      INVALID_SUBJECT_STATE: "INVALID_SUBJECT_STATE",
    });
  });
});

describe("self-approval is blocked in the UI", () => {
  it("hides the decision buttons when the current user is the submitter", () => {
    withClient(
      <DecisionActions approval={detail()} canDecide currentUserId="u-sub" />,
    );
    expect(screen.getByText(/You submitted this item/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Reject/ })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Request changes/ }),
    ).toBeNull();
  });

  it("also blocks reversing one's own auto-approved item", () => {
    withClient(
      <DecisionActions
        approval={detail({ current_status: "auto_approved" })}
        canDecide
        currentUserId="u-sub"
      />,
    );
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });

  it("keeps the buttons for a different approver", () => {
    withClient(
      <DecisionActions approval={detail()} canDecide currentUserId="u-mgr" />,
    );
    expect(screen.getByRole("button", { name: /Approve/ })).toBeTruthy();
    expect(screen.queryByText(/You submitted this item/i)).toBeNull();
  });

  it("DecisionPanel passes the current user through to the actions", () => {
    withClient(
      <DecisionPanel
        approvalId="ap-1"
        detail={detail()}
        isLoading={false}
        isError={false}
        onRetry={vi.fn()}
        onBack={vi.fn()}
        canDecide
        currentUserId="u-sub"
      />,
    );
    expect(screen.getByText(/You submitted this item/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
  });
});

describe("decision errors surface the server's message", () => {
  it("403 FORBIDDEN shows a specific title with the envelope message", async () => {
    patch.mockRejectedValue(
      apiError("FORBIDDEN", "You cannot decide your own submission.", 403),
    );
    withClient(
      <DecisionActions approval={detail()} canDecide currentUserId="u-mgr" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith({
        variant: "error",
        title: "You can’t decide this approval",
        description: "You cannot decide your own submission.",
      }),
    );
  });

  it("400 INVALID_SUBJECT_STATE shows a specific title with the envelope message", async () => {
    patch.mockRejectedValue(
      apiError(
        "INVALID_SUBJECT_STATE",
        "The trip is no longer awaiting approval.",
        400,
      ),
    );
    withClient(
      <DecisionActions approval={detail()} canDecide currentUserId="u-mgr" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith({
        variant: "error",
        title: "No longer awaiting a decision",
        description: "The trip is no longer awaiting approval.",
      }),
    );
  });

  it("INVALID_SUBJECT_STATE invalidates the stale detail and queue", async () => {
    patch.mockRejectedValue(
      apiError("INVALID_SUBJECT_STATE", "Moved on.", 400),
    );
    const qc = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    });
    const spy = vi.spyOn(qc, "invalidateQueries");
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const { result } = renderHook(() => useDecideApproval(), { wrapper });
    await act(async () => {
      await result.current
        .mutateAsync({ id: "ap-1", decision: "approve", version: 4 })
        .catch(() => {});
    });
    expect(spy).toHaveBeenCalledWith({
      queryKey: approvalKeys.detail("ap-1"),
    });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["approvals"] });
  });
});
