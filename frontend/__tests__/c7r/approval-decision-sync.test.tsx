/**
 * C7R issue 3 —
 *  (a) after Approve/Reject the panel must leave "Pending" and drop the
 *      decision buttons straight from the PATCH response, the queue row must
 *      move, and every affected view (queue, detail, dashboard counts, subject
 *      report) must be invalidated — also when the PATCH fails, because a lost
 *      response may hide a decision that committed;
 *  (b) the approval detail/queue reads retry transient failures (network,
 *      502/503/504) with backoff, never 4xx.
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
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { showToast, patch, get } = vi.hoisted(() => ({
  showToast: vi.fn(),
  patch: vi.fn(),
  get: vi.fn(),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get, post: vi.fn(), patch },
}));

import { DecisionPanel } from "@/components/approvals/decision-panel";
import {
  approvalKeys,
  useApproval,
  useDecideApproval,
} from "@/lib/query/approval-hooks";
import {
  isTransientError,
  retryTransient,
  transientRetryDelay,
} from "@/lib/query/retry";
import { ApiError, type PaginatedResponse } from "@/lib/types/api";
import type { ApprovalDetail, ApprovalListItem } from "@/lib/types/approvals";

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

function listItem(over: Partial<ApprovalListItem> = {}): ApprovalListItem {
  return {
    id: "ap-1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "r1",
    submitter_id: "u-sub",
    submitter_name: "Alex",
    subject_title: "NYC",
    subject_amount: "10.00",
    currency: "USD",
    current_status: "pending",
    is_auto_approved: false,
    risk_level: "medium",
    auto_approve_rationale: null,
    created_at: "2026-06-12T00:00:00Z",
    version: 4,
    ...over,
  };
}

const LIST_KEY = ["approvals", { size: 100 }] as const;

function makeClient() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity },
      mutations: { retry: false },
    },
  });
  qc.setQueryData(approvalKeys.detail("ap-1"), detail());
  qc.setQueryData<PaginatedResponse<ApprovalListItem>>(LIST_KEY, {
    items: [listItem(), listItem({ id: "ap-2" })],
    total: 2,
    page: 1,
    size: 100,
    pages: 1,
  } as unknown as PaginatedResponse<ApprovalListItem>);
  qc.setQueryData(["analytics", "overview"], { manager: { pending: 1 } });
  qc.setQueryData(["expense-report", "r1"], { id: "r1", status: "submitted" });
  return qc;
}

/** Live panel bound to the detail query, as on the approvals screen. */
function Panel() {
  const q = useApproval("ap-1");
  return (
    <DecisionPanel
      approvalId="ap-1"
      detail={q.data ?? null}
      isLoading={q.isLoading}
      isError={q.isError}
      onRetry={() => q.refetch()}
      onBack={() => {}}
      canDecide
      currentUserId="u-mgr"
    />
  );
}

afterEach(cleanup);
beforeEach(() => {
  showToast.mockReset();
  patch.mockReset();
  get.mockReset();
});

describe("C7R-3a decision updates the UI from the PATCH response", () => {
  it("Approve → panel shows Approved, buttons gone, queue row moved, views invalidated", async () => {
    const qc = makeClient();
    const approved = detail({ current_status: "approved", version: 5 });
    patch.mockResolvedValue(approved);
    // Any refetch returns the committed server state.
    get.mockResolvedValue(approved);

    render(
      <QueryClientProvider client={qc}>
        <Panel />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull(),
    );
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
    expect(screen.queryByText("Pending")).toBeNull();

    const page =
      qc.getQueryData<PaginatedResponse<ApprovalListItem>>(LIST_KEY)!;
    expect(page.items.find((i) => i.id === "ap-1")?.current_status).toBe(
      "approved",
    );
    expect(page.items.find((i) => i.id === "ap-1")?.version).toBe(5);
    expect(page.items.find((i) => i.id === "ap-2")?.current_status).toBe(
      "pending",
    );
    await waitFor(() => {
      expect(qc.getQueryState(LIST_KEY)?.isInvalidated).toBe(true);
      expect(qc.getQueryState(["analytics", "overview"])?.isInvalidated).toBe(
        true,
      );
      expect(qc.getQueryState(["expense-report", "r1"])?.isInvalidated).toBe(
        true,
      );
    });
  });

  it("a slow pre-decision GET cannot flip the panel back to Pending", async () => {
    const qc = makeClient();
    let resolveStale!: (d: ApprovalDetail) => void;
    // A detail fetch with no mounted observer (e.g. the user navigated within
    // the panel) is still in flight when the PATCH lands. Invalidation does not
    // refetch inactive queries, so only an explicit cancel stops it landing.
    void qc.prefetchQuery({
      queryKey: approvalKeys.detail("ap-1"),
      queryFn: () =>
        new Promise<ApprovalDetail>((r) => {
          resolveStale = r;
        }),
      staleTime: 0,
    });
    await waitFor(() => expect(resolveStale).toBeTypeOf("function"));

    const approved = detail({ current_status: "approved", version: 5 });
    patch.mockResolvedValue(approved);
    get.mockResolvedValue(approved);
    const { result } = renderHook(() => useDecideApproval(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });
    await result.current.mutateAsync({
      id: "ap-1",
      decision: "approve",
      version: 4,
    });
    resolveStale(detail()); // stale "pending" arrives late
    await new Promise((r) => setTimeout(r, 0));
    expect(
      qc.getQueryData<ApprovalDetail>(approvalKeys.detail("ap-1"))
        ?.current_status,
    ).toBe("approved");
  });

  it("a network failure on PATCH still re-syncs detail + queue", async () => {
    const qc = makeClient();
    patch.mockRejectedValue(new TypeError("Failed to fetch"));
    get.mockResolvedValue(detail({ current_status: "approved", version: 5 }));
    const { result } = renderHook(() => useDecideApproval(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });
    await expect(
      result.current.mutateAsync({
        id: "ap-1",
        decision: "approve",
        version: 4,
      }),
    ).rejects.toThrow();
    await waitFor(() => {
      expect(qc.getQueryState(LIST_KEY)?.isInvalidated).toBe(true);
      expect(qc.getQueryState(approvalKeys.detail("ap-1"))?.isInvalidated).toBe(
        true,
      );
    });
  });

  it("a PATCH response older than the cached detail does not overwrite it", async () => {
    const qc = makeClient();
    // A refetch already landed a NEWER version (e.g. a second approver step).
    qc.setQueryData(
      approvalKeys.detail("ap-1"),
      detail({ current_status: "approved", version: 6 }),
    );
    patch.mockResolvedValue(detail({ current_status: "pending", version: 5 }));
    get.mockResolvedValue(detail({ current_status: "approved", version: 6 }));
    const { result } = renderHook(() => useDecideApproval(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });
    await result.current.mutateAsync({
      id: "ap-1",
      decision: "approve",
      version: 4,
    });
    expect(
      qc.getQueryData<ApprovalDetail>(approvalKeys.detail("ap-1"))?.version,
    ).toBe(6);
  });
});

describe("C7R-3b transient retry for approval reads", () => {
  const err = (status: number) =>
    new ApiError({ code: "X", message: "x", correlation_id: "c" }, status);

  it("classifies network + 502/503/504 as transient, never 4xx / 500", () => {
    expect(isTransientError(new TypeError("Failed to fetch"))).toBe(true);
    for (const s of [502, 503, 504])
      expect(isTransientError(err(s))).toBe(true);
    for (const s of [400, 401, 403, 404, 409, 422, 429, 500]) {
      expect(isTransientError(err(s))).toBe(false);
    }
    expect(retryTransient(2, err(503))).toBe(true);
    expect(retryTransient(3, err(503))).toBe(false);
  });

  it("backs off exponentially with a cap", () => {
    expect(transientRetryDelay(0, () => 0)).toBe(250);
    expect(transientRetryDelay(0, () => 1)).toBe(500);
    expect(transientRetryDelay(2, () => 1)).toBe(2000);
    expect(transientRetryDelay(10, () => 1)).toBe(8000);
  });

  function hookClient() {
    // retry:false at the client level — the hook's own policy must win.
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    return ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
  }

  it("first-load 503 then success → detail loads without an error state", async () => {
    get
      .mockRejectedValueOnce(err(503))
      .mockResolvedValueOnce(detail({ id: "ap-9" }));
    const { result } = renderHook(() => useApproval("ap-9"), {
      wrapper: hookClient(),
    });
    await waitFor(() => expect(result.current.data?.id).toBe("ap-9"), {
      timeout: 3000,
    });
    expect(result.current.isError).toBe(false);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("a 404 is not retried", async () => {
    get.mockRejectedValue(err(404));
    const { result } = renderHook(() => useApproval("ap-404"), {
      wrapper: hookClient(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(get).toHaveBeenCalledTimes(1);
  });
});
