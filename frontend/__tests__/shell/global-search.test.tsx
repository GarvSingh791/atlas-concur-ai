import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ApiError } from "@/lib/types/api";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";
import type { SearchResponse } from "@/lib/types/shell";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));
const apiGet = vi.hoisted(() => vi.fn());
vi.mock("@/lib/api/client", () => ({ api: { get: apiGet } }));

import { useSession } from "@/lib/auth/session-provider";
import {
  GlobalSearch,
  SEARCH_DEBOUNCE_MS,
} from "@/components/atlas/global-search";

function session(role: RoleKey) {
  vi.mocked(useSession).mockReturnValue({
    user: { id: "me", role },
    role,
    permissions: ROLE_PERMISSIONS[role],
  } as never);
}

const RESPONSE: SearchResponse = {
  query: "lis",
  groups: [
    {
      key: "reports",
      label: "Expense reports",
      truncated: false,
      items: [
        {
          id: "r1",
          title: "Lisbon summit",
          subtitle: "120.00 USD",
          status: "submitted",
          link: "/expenses?report=r1",
          fallback_link: "/approvals?approval=a1",
        },
      ],
    },
    {
      key: "policy_rules",
      label: "Policy rules",
      truncated: false,
      items: [
        {
          id: "p1",
          title: "Hotel cap — Lisbon",
          subtitle: null,
          status: "active",
          link: "/policy",
          fallback_link: null,
        },
      ],
    },
  ],
};

function renderSearch() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <GlobalSearch />
    </QueryClientProvider>,
  );
}

const input = () => screen.getByTestId("global-search-input");

function type(value: string) {
  fireEvent.focus(input());
  fireEvent.change(input(), { target: { value } });
}

const settle = () =>
  act(() => new Promise((r) => setTimeout(r, SEARCH_DEBOUNCE_MS + 60)));

beforeEach(() => {
  push.mockReset();
  apiGet.mockReset();
  apiGet.mockResolvedValue(RESPONSE);
  session("manager");
});
afterEach(cleanup);

describe("GlobalSearch", () => {
  it("never queries below two characters and says so", async () => {
    renderSearch();
    type("l");
    await settle();
    expect(apiGet).not.toHaveBeenCalled();
    expect(screen.getByTestId("global-search-status").textContent).toMatch(
      /at least 2 characters/,
    );
    type("  l ");
    await settle();
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("debounces: one request for the settled value", async () => {
    renderSearch();
    type("li");
    type("lis");
    expect(apiGet).not.toHaveBeenCalled();
    await settle();
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1));
    expect(apiGet.mock.calls[0][0]).toBe("/search");
    expect(apiGet.mock.calls[0][1].params).toEqual({ q: "lis", limit: 5 });
  });

  it("renders grouped results and opens the active one with arrows + Enter", async () => {
    renderSearch();
    type("lis");
    await settle();
    await screen.findByText("Lisbon summit");
    expect(screen.getByText("Expense reports")).toBeTruthy();
    // A manager cannot open /policy: the rule is shown but not navigable.
    const rule = screen.getByTestId("search-result-policy_rules-p1");
    expect(rule.getAttribute("aria-disabled")).toBe("true");

    fireEvent.keyDown(input(), { key: "ArrowDown" });
    const report = screen.getByTestId("search-result-reports-r1");
    expect(report.getAttribute("aria-selected")).toBe("true");
    expect(input().getAttribute("aria-activedescendant")).toBe(report.id);
    // ArrowDown wraps over the non-navigable rule back to the report.
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(report.getAttribute("aria-selected")).toBe("true");

    fireEvent.keyDown(input(), { key: "Enter" });
    // The manager lands on the (read-only for non-owners) report view.
    expect(push).toHaveBeenCalledWith("/expenses?report=r1");
  });

  it("finance is sent to the approval view instead of a gated page", async () => {
    session("finance_admin");
    renderSearch();
    type("lis");
    await settle();
    fireEvent.click(await screen.findByText("Lisbon summit"));
    expect(push).toHaveBeenCalledWith("/approvals?approval=a1");
  });

  it("clicking a non-navigable result does nothing", async () => {
    renderSearch();
    type("lis");
    await settle();
    fireEvent.click(await screen.findByText("Hotel cap — Lisbon"));
    expect(push).not.toHaveBeenCalled();
  });

  it("shows empty and error states", async () => {
    apiGet.mockResolvedValueOnce({ query: "zz", groups: [] });
    renderSearch();
    type("zz");
    await settle();
    await waitFor(() =>
      expect(screen.getByTestId("global-search-status").textContent).toMatch(
        /No results for “zz”/,
      ),
    );
    cleanup();

    apiGet.mockRejectedValueOnce(
      new ApiError(
        { code: "INTERNAL_SERVER_ERROR", message: "x", correlation_id: "c" },
        500,
      ),
    );
    renderSearch();
    type("boom");
    await settle();
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toMatch(/unavailable/),
    );
  });

  it("'/' focuses the box from the page; Escape closes then blurs", async () => {
    renderSearch();
    fireEvent.keyDown(document.body, { key: "/" });
    expect(document.activeElement).toBe(input());

    type("lis");
    await settle();
    await screen.findByText("Lisbon summit");
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(screen.queryByTestId("global-search-panel")).toBeNull();
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(document.activeElement).not.toBe(input());
    expect((input() as HTMLInputElement).value).toBe("");
  });

  it("'/' typed inside another text field is left alone", () => {
    renderSearch();
    const other = document.createElement("input");
    document.body.appendChild(other);
    other.focus();
    fireEvent.keyDown(other, { key: "/" });
    expect(document.activeElement).toBe(other);
    other.remove();
  });
});
