/**
 * QA round (browser QA of the product-gaps build) — approvals screen.
 *
 *  - Defect 2: an inverted submitted date range is caught in the filter bar
 *    (inline message, NO request); any 400 the server still returns is shown
 *    with the server's own validation message, not "Something went wrong".
 *  - Defect 7: a withdrawn report shows as "Withdrawn" (badge + status filter).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get, replace } = vi.hoisted(() => ({
  get: vi.fn(),
  replace: vi.fn(),
}));
let search = "";
vi.mock("next/navigation", () => ({
  usePathname: () => "/approvals",
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(search),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get, post: vi.fn(), patch: vi.fn() },
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({
    user: { id: "fin-1" },
    role: "finance_admin",
    hasPermission: () => true,
  }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import { ApprovalsScreen } from "@/components/approvals/approvals-screen";
import { ApprovalQueue } from "@/components/approvals/approval-queue";
import {
  APPROVAL_STATUS_OPTIONS,
  dateRangeError,
  filtersFromSearchParams,
} from "@/lib/approvals/filter-params";
import { ApiError } from "@/lib/types/api";
import type { ApprovalListItem } from "@/lib/types/approvals";

const EMPTY = { items: [], total: 0, page: 1, size: 100, pages: 0 };

function renderScreen() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalsScreen />
    </QueryClientProvider>,
  );
}

function listCalls() {
  return get.mock.calls.filter(([path]) => path === "/approvals");
}

beforeEach(() => {
  get.mockReset();
  replace.mockReset();
  get.mockImplementation((path: string) => {
    if (path === "/approvals/exception-types") return Promise.resolve([]);
    if (path === "/approvals/departments") return Promise.resolve([]);
    return Promise.resolve(EMPTY);
  });
});
afterEach(cleanup);

describe("defect 2 — inverted date range", () => {
  it("dateRangeError flags only a from-after-to range", () => {
    expect(dateRangeError({})).toBeNull();
    expect(dateRangeError({ submitted_from: "2026-11-14" })).toBeNull();
    expect(
      dateRangeError({
        submitted_from: "2026-11-14",
        submitted_to: "2026-11-14",
      }),
    ).toBeNull();
    expect(
      dateRangeError({
        submitted_from: "2026-11-20",
        submitted_to: "2026-11-10",
      }),
    ).toMatch(/on or before/);
  });

  it("shows an inline message and sends no list request", async () => {
    search = "submitted_from=2026-11-20&submitted_to=2026-11-10";
    renderScreen();
    const alert = await screen.findByTestId("approval-date-range-error");
    expect(alert.getAttribute("role")).toBe("alert");
    expect(alert.textContent).toMatch(
      /“Submitted from” must be on or before “Submitted to”/,
    );
    expect(
      screen.getByLabelText("Submitted from").getAttribute("aria-invalid"),
    ).toBe("true");
    // The other filter options still load, but the list is never requested.
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/approvals/departments"),
    );
    expect(listCalls()).toHaveLength(0);
    expect(screen.queryByText("Couldn’t load approvals")).toBeNull();
  });

  it("surfaces the server's validation message for a 400", async () => {
    search = "submitted_from=2026-11-01";
    get.mockImplementation((path: string) => {
      if (path === "/approvals")
        return Promise.reject(
          new ApiError(
            {
              code: "BAD_REQUEST",
              message: "Invalid exception_type 'x'.",
              correlation_id: "c",
            },
            400,
          ),
        );
      return Promise.resolve([]);
    });
    renderScreen();
    expect(await screen.findByText("Invalid exception_type 'x'.")).toBeTruthy();
    expect(screen.getByText("Couldn’t apply these filters")).toBeTruthy();
    expect(
      screen.queryByText("Something went wrong fetching your queue."),
    ).toBeNull();
  });

  it("keeps the generic message for a server error", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/approvals")
        return Promise.reject(
          new ApiError(
            { code: "INTERNAL", message: "boom", correlation_id: "c" },
            500,
          ),
        );
      return Promise.resolve([]);
    });
    search = "";
    renderScreen();
    expect(
      await screen.findByText("Something went wrong fetching your queue."),
    ).toBeTruthy();
    expect(screen.queryByText("boom")).toBeNull();
  });
});

function item(over: Partial<ApprovalListItem>): ApprovalListItem {
  return {
    id: "a1",
    organization_id: "org",
    subject_type: "expense_report",
    subject_id: "s1",
    submitter_id: "u1",
    submitter_name: "Alex Employee",
    subject_title: "B-return",
    subject_amount: "5.60",
    currency: "USD",
    current_status: "changes_requested",
    is_auto_approved: false,
    risk_level: "low",
    auto_approve_rationale: null,
    created_at: "2026-11-14T00:00:00Z",
    version: 1,
    ...over,
  };
}

describe("defect 7 — withdrawn reports", () => {
  it("labels a withdrawn return 'Withdrawn', a live return 'Changes requested'", () => {
    render(
      <ApprovalQueue
        items={[
          item({
            id: "w",
            subject_title: "Withdrawn one",
            subject_withdrawn: true,
            display_status: "withdrawn",
          }),
          item({ id: "r", subject_title: "Returned one" }),
        ]}
        isLoading={false}
        isError={false}
        onRetry={() => {}}
        selectedId={null}
        viewedIds={new Set()}
        onSelect={() => {}}
      />,
    );
    const withdrawn = screen
      .getByText("Withdrawn one")
      .closest("button") as HTMLElement;
    expect(within(withdrawn).getByText("Withdrawn")).toBeTruthy();
    expect(within(withdrawn).queryByText("Changes requested")).toBeNull();
    const returned = screen
      .getByText("Returned one")
      .closest("button") as HTMLElement;
    expect(within(returned).getByText("Changes requested")).toBeTruthy();
  });

  it("offers Withdrawn as a status filter and keeps it from the URL", () => {
    expect(
      APPROVAL_STATUS_OPTIONS.find((o) => o.value === "withdrawn")?.label,
    ).toBe("Withdrawn");
    expect(
      filtersFromSearchParams(new URLSearchParams("current_status=withdrawn")),
    ).toEqual({ current_status: "withdrawn" });
  });
});
