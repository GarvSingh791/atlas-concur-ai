/**
 * UAT2 Issue 7 — a manager's own Trips and Expenses pages listed employees'
 * items. The list endpoints return the role's READ scope (manager = self +
 * direct reports); the personal pages now ALWAYS send `mine=true` so they show
 * only the caller's own items. Team review stays on /approvals.
 *
 * Also: Analytics is finance-only in the nav (managers only ever saw a
 * "finance administrators" notice there).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { renderHook, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { get, post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import { expenseKeys, useExpenseReports } from "@/lib/query/expense-hooks";
import { tripQueryKeys, useTrips } from "@/lib/query/trip-hooks";
import * as analytics from "@/lib/query/analytics-hooks";
import {
  canAccessPath,
  getVisibleNavItems,
  getMobileTabItems,
} from "@/lib/nav-config";
import { isKnownNonOwner, isRecordOwner } from "@/lib/auth/ownership";
import { ROLE_PERMISSIONS } from "@/lib/types/identity";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const EMPTY_PAGE = { items: [], total: 0, page: 1, size: 20, pages: 0 };

beforeEach(() => {
  get.mockReset();
  get.mockResolvedValue(EMPTY_PAGE);
});
afterEach(cleanup);

describe("personal list hooks always send mine=true", () => {
  it("useExpenseReports (Expenses page / move targets) keeps the caller's params", async () => {
    renderHook(
      () =>
        useExpenseReports({
          page: 2,
          size: 20,
          status: "draft",
          sort: "-created_at",
        }),
      { wrapper },
    );
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(get).toHaveBeenCalledWith("/expense-reports", {
      params: {
        page: 2,
        size: 20,
        status: "draft",
        sort: "-created_at",
        mine: true,
      },
    });
  });

  it("useExpenseReports with no params still narrows", async () => {
    renderHook(() => useExpenseReports(), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(get.mock.calls[0][1]).toEqual({ params: { mine: true } });
  });

  it("useTrips (My Trips / new-report trip picker) sends size 50 + mine", async () => {
    renderHook(() => useTrips(), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(get).toHaveBeenCalledWith("/trips", {
      params: { size: 50, mine: true },
    });
  });

  it("employee-dashboard widgets send mine too", async () => {
    renderHook(() => analytics.useTrips({ size: 5 }), { wrapper });
    renderHook(() => analytics.useExpenseReports({ size: 5 }), { wrapper });
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(get).toHaveBeenCalledWith("/trips", {
      params: { size: 5, mine: true },
    });
    expect(get).toHaveBeenCalledWith("/expense-reports", {
      params: { size: 5, mine: true },
    });
  });

  it("list keys stay under the prefixes every invalidation uses", () => {
    expect(expenseKeys.list({ page: 1 })[0]).toBe("expense-reports");
    expect(tripQueryKeys.list()[0]).toBe("trips");
    expect(expenseKeys.list()).toContain("mine");
    expect(tripQueryKeys.list()).toContain("mine");
  });
});

describe("isRecordOwner / isKnownNonOwner", () => {
  it("owner only when the session user id matches; fails closed", () => {
    expect(isRecordOwner({ id: "emp" }, "emp")).toBe(true);
    expect(isRecordOwner({ id: "mgr" }, "emp")).toBe(false);
    expect(isRecordOwner(null, "emp")).toBe(false);
    expect(isRecordOwner(undefined, undefined)).toBe(false);
    expect(isRecordOwner({ id: "" }, "")).toBe(false);
  });
  it("a known non-owner needs a loaded session", () => {
    expect(isKnownNonOwner({ id: "mgr" }, "emp")).toBe(true);
    expect(isKnownNonOwner({ id: "emp" }, "emp")).toBe(false);
    expect(isKnownNonOwner(null, "emp")).toBe(false);
  });
});

describe("Analytics is finance-only in the nav", () => {
  it("a manager no longer gets the Analytics link or route", () => {
    const perms = ROLE_PERMISSIONS.manager;
    const hrefs = getVisibleNavItems(perms, "manager").map((i) => i.href);
    expect(hrefs).not.toContain("/analytics");
    expect(canAccessPath("/analytics", perms, "manager")).toBe(false);
    expect(
      getMobileTabItems(perms, "manager").map((i) => i.href),
    ).not.toContain("/analytics");
    // Personal + team surfaces are still there.
    expect(hrefs).toEqual(
      expect.arrayContaining(["/approvals", "/expenses", "/trips"]),
    );
  });

  it("finance keeps it; employees never had it", () => {
    expect(
      getVisibleNavItems(ROLE_PERMISSIONS.finance_admin, "finance_admin").map(
        (i) => i.href,
      ),
    ).toContain("/analytics");
    expect(
      getVisibleNavItems(ROLE_PERMISSIONS.employee, "employee").map(
        (i) => i.href,
      ),
    ).not.toContain("/analytics");
  });
});

// ---------------------------------------------------------------------------
// Deploy window: a backend that predates `mine=true` ignores it and returns the
// role's full read scope. The personal lists still keep only the caller's items.
// ---------------------------------------------------------------------------
import { keepCallerItems } from "@/lib/query/own-items";
import { useAuthStore } from "@/stores/auth-store";

describe("personal lists against a backend that ignores mine=true", () => {
  const ME = "user-me";
  const page = <T,>(items: T[]) => ({
    items,
    total: items.length,
    page: 1,
    size: 20,
    pages: 1,
  });

  afterEach(() => {
    useAuthStore.setState({ user: null });
  });

  it("keepCallerItems drops items owned by someone else and fixes the total", () => {
    const p = page([
      { id: "a", owner_id: ME },
      { id: "b", owner_id: "teammate" },
    ]);
    const out = keepCallerItems(p, (r) => r.owner_id, ME);
    expect(out.items.map((r) => r.id)).toEqual(["a"]);
    expect(out.total).toBe(1);
  });

  it("keepCallerItems is a no-op for an all-own page and with no session user", () => {
    const own = page([{ id: "a", owner_id: ME }]);
    expect(keepCallerItems(own, (r) => r.owner_id, ME)).toBe(own);
    const mixed = page([{ id: "b", owner_id: "teammate" }]);
    expect(keepCallerItems(mixed, (r) => r.owner_id, null)).toBe(mixed);
  });

  it("useExpenseReports shows only the caller's reports", async () => {
    useAuthStore.setState({ user: { id: ME } as never });
    get.mockResolvedValueOnce(
      page([
        { id: "mine", owner_id: ME },
        { id: "team", owner_id: "teammate" },
      ]),
    );
    const { result } = renderHook(() => useExpenseReports(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items.map((r) => r.id)).toEqual(["mine"]);
  });

  it("useTrips shows only the caller's trips", async () => {
    useAuthStore.setState({ user: { id: ME } as never });
    get.mockResolvedValueOnce(
      page([
        { id: "mine", traveler_id: ME },
        { id: "team", traveler_id: "teammate" },
      ]),
    );
    const { result } = renderHook(() => useTrips(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.items.map((t) => t.id)).toEqual(["mine"]);
  });
});
