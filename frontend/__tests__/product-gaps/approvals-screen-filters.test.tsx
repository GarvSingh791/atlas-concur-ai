/**
 * Product gap 6 — the approvals screen sends the URL filters to the SERVER and
 * writes filter changes back to the URL (linkable views).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { get, replace, session } = vi.hoisted(() => ({
  get: vi.fn(),
  replace: vi.fn(),
  session: { perms: [] as string[] },
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
    hasPermission: (p: string) => session.perms.includes(p),
  }),
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));

import { ApprovalsScreen } from "@/components/approvals/approvals-screen";

const EMPTY = { items: [], total: 0, page: 1, size: 100, pages: 0 };

function renderScreen() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ApprovalsScreen />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  get.mockReset();
  replace.mockReset();
  session.perms = [
    "approval.read",
    "approval.decide.reports",
    "expense.read.org",
  ];
  get.mockImplementation((path: string) => {
    if (path === "/approvals/exception-types")
      return Promise.resolve([
        {
          value: "missing_receipt",
          label: "Missing receipt",
          kind: "flag",
          count: 2,
        },
      ]);
    if (path === "/approvals/departments") return Promise.resolve(["Sales"]);
    return Promise.resolve(EMPTY);
  });
});
afterEach(cleanup);

describe("ApprovalsScreen server-side filters", () => {
  it("sends only the allow-listed URL filters to GET /approvals", async () => {
    search =
      "department=Sales&current_status=bogus&evil=1&submitted_from=2026-04-01";
    renderScreen();
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/approvals", {
        params: {
          size: 100,
          department: "Sales",
          submitted_from: "2026-04-01",
        },
      }),
    );
  });

  it("writes a filter change to the URL and clears it", async () => {
    search = "department=Sales";
    renderScreen();
    const select = await screen.findByLabelText("Exception type");
    await screen.findByRole("option", { name: "Missing receipt (2)" });
    fireEvent.change(select, { target: { value: "missing_receipt" } });
    expect(replace).toHaveBeenLastCalledWith(
      "/approvals?exception_type=missing_receipt&department=Sales",
      { scroll: false },
    );
    fireEvent.click(screen.getByTestId("approval-filters-clear"));
    expect(replace).toHaveBeenLastCalledWith("/approvals", { scroll: false });
  });

  it("shows a filtered empty state with the result count", async () => {
    search = "department=Sales";
    renderScreen();
    expect(await screen.findByText("No matching approvals")).toBeTruthy();
    expect(screen.getByTestId("approval-result-count").textContent).toContain(
      "0 results",
    );
  });

  it("offers the export to finance only", async () => {
    search = "";
    renderScreen();
    expect(await screen.findByTestId("export-csv")).toBeTruthy();
    cleanup();
    session.perms = ["approval.read", "approval.decide.reports"];
    renderScreen();
    await screen.findByTestId("approval-filters");
    expect(screen.queryByTestId("export-csv")).toBeNull();
  });
});
