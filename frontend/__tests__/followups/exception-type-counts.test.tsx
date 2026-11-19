/**
 * Follow-up (UI polish) — the exception-type counts in the approvals filter
 * bar follow the OTHER active filters: GET /approvals/exception-types is sent
 * the same filters as the list (minus the selected exception type itself), and
 * an inverted date range sends nothing.
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
  session.perms = ["approval.read", "approval.decide.reports"];
  get.mockImplementation((path: string, opts?: { params?: object }) => {
    if (path === "/approvals/exception-types") {
      const sales =
        (opts?.params as Record<string, string> | undefined)?.department ===
        "Sales";
      return Promise.resolve([
        {
          value: "amount_cap",
          label: "Policy cap",
          kind: "rule",
          count: sales ? 0 : 1,
        },
      ]);
    }
    if (path === "/approvals/departments") return Promise.resolve(["Sales"]);
    return Promise.resolve(EMPTY);
  });
});
afterEach(cleanup);

describe("exception-type counts follow the active filters", () => {
  it("sends the other filters (not the chosen type) with the counts request", async () => {
    search =
      "department=Sales&exception_type=amount_cap&current_status=pending&submitted_from=2026-04-01";
    renderScreen();
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/approvals/exception-types", {
        params: {
          current_status: "pending",
          department: "Sales",
          submitted_from: "2026-04-01",
        },
      }),
    );
    expect(
      await screen.findByRole("option", { name: "Policy cap (0)" }),
    ).toBeTruthy();
  });

  it("unfiltered: no params, scope-wide counts", async () => {
    search = "";
    renderScreen();
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith("/approvals/exception-types", {
        params: {},
      }),
    );
    expect(
      await screen.findByRole("option", { name: "Policy cap (1)" }),
    ).toBeTruthy();
  });

  it("an inverted date range sends no counts request", async () => {
    search = "submitted_from=2026-05-02&submitted_to=2026-05-01";
    renderScreen();
    await screen.findByTestId("approval-filters");
    expect(
      get.mock.calls.some(([p]) => p === "/approvals/exception-types"),
    ).toBe(false);
  });
});
