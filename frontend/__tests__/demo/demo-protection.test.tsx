/**
 * Phase 11 — public-demo UX: protected demo accounts in the Users admin
 * (lock badge + tooltip, edit / deactivate disabled) and the dismissible
 * "Demo environment" banner (DEMO_MODE via GET /public/config).
 * The UI is only a hint; the server enforces every protection.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as React from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";

import UsersPage from "@/app/(app)/users/page";
import {
  DEMO_BANNER_DISMISSED_KEY,
  DemoBanner,
} from "@/components/atlas/demo-banner";
import { ROLE_PERMISSIONS } from "@/lib/types/identity";
import type { AdminUser } from "@/lib/types/shell";

vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast: vi.fn() }),
}));
const hooks = vi.hoisted(() => ({
  useAdminUsers: vi.fn(),
  useManagerOptions: vi.fn(),
  useCreateUser: vi.fn(),
  useUpdateUser: vi.fn(),
  useSetUserActive: vi.fn(),
  useGrades: vi.fn(),
  useOrganizationLoginCode: vi.fn(),
}));
vi.mock("@/lib/query/user-admin-hooks", () => hooks);
const cfg = vi.hoisted(() => ({ usePublicConfig: vi.fn() }));
vi.mock("@/lib/query/public-config-hooks", () => cfg);

import { useSession } from "@/lib/auth/session-provider";

function user(over: Partial<AdminUser>): AdminUser {
  return {
    id: "u1",
    name: "Alex Employee",
    email: "employee@atlas-demo.internal",
    role: "employee",
    department: "Engineering",
    manager_id: null,
    manager_name: null,
    status: "active",
    organization_id: "o",
    created_at: "2026-01-01T00:00:00Z",
    grade: null,
    ...over,
  };
}

const USERS = [
  user({ id: "demo", demo_protected: true }),
  user({
    id: "demo-off",
    name: "Brenda Taylor",
    email: "manager2@atlas-demo.internal",
    status: "inactive",
    demo_protected: true,
  }),
  user({ id: "qa", name: "Visitor QA", email: "qa@example.test" }),
];

beforeEach(() => {
  vi.mocked(useSession).mockReturnValue({
    user: { id: "fin", role: "finance_admin" },
    role: "finance_admin",
    permissions: ROLE_PERMISSIONS.finance_admin,
  } as never);
  hooks.useAdminUsers.mockReturnValue({
    data: { items: USERS, total: USERS.length, page: 1, size: 20, pages: 1 },
    isLoading: false,
    isError: false,
    refetch: vi.fn(),
  });
  hooks.useManagerOptions.mockReturnValue({ data: [], isLoading: false });
  hooks.useCreateUser.mockReturnValue({ mutate: vi.fn(), isPending: false });
  hooks.useUpdateUser.mockReturnValue({ mutate: vi.fn(), isPending: false });
  hooks.useSetUserActive.mockReturnValue({ mutate: vi.fn(), isPending: false });
  hooks.useGrades.mockReturnValue({ data: [], isLoading: false });
  hooks.useOrganizationLoginCode.mockReturnValue({ data: undefined });
  window.sessionStorage.clear();
});
afterEach(cleanup);

describe("Users admin — protected demo accounts", () => {
  it("shows a lock badge with an explanation on protected rows only", () => {
    render(<UsersPage />);
    const demo = within(screen.getByTestId("user-row-demo"));
    const badge = demo.getByTestId("demo-lock-badge");
    expect(badge.getAttribute("aria-label")).toContain(
      "protected demo account",
    );
    expect(
      within(screen.getByTestId("user-row-qa")).queryByTestId(
        "demo-lock-badge",
      ),
    ).toBeNull();
  });

  it("disables edit and deactivate for a protected account", () => {
    render(<UsersPage />);
    const demo = within(screen.getByTestId("user-row-demo"));
    const edit = demo.getByRole("button", { name: "Edit Alex Employee" });
    const deactivate = demo.getByRole("button", {
      name: "Deactivate Alex Employee",
    });
    expect((edit as HTMLButtonElement).disabled).toBe(true);
    expect((deactivate as HTMLButtonElement).disabled).toBe(true);
  });

  it("keeps reactivation available for an inactive protected account", () => {
    render(<UsersPage />);
    const row = within(screen.getByTestId("user-row-demo-off"));
    const reactivate = row.getByRole("button", {
      name: "Reactivate Brenda Taylor",
    });
    expect((reactivate as HTMLButtonElement).disabled).toBe(false);
    expect(
      (
        row.getByRole("button", {
          name: "Edit Brenda Taylor",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });

  it("leaves visitor-created users fully manageable", () => {
    render(<UsersPage />);
    const qa = within(screen.getByTestId("user-row-qa"));
    for (const name of ["Edit Visitor QA", "Deactivate Visitor QA"]) {
      expect(
        (qa.getByRole("button", { name }) as HTMLButtonElement).disabled,
      ).toBe(false);
    }
  });
});

describe("Demo environment banner", () => {
  it("is hidden when the server is not in demo mode", () => {
    cfg.usePublicConfig.mockReturnValue({ data: { demo_mode: false } });
    render(<DemoBanner />);
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });

  it("is hidden while the config is unknown", () => {
    cfg.usePublicConfig.mockReturnValue({ data: undefined });
    render(<DemoBanner />);
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });

  it("explains the shared demo and the periodic reset", () => {
    cfg.usePublicConfig.mockReturnValue({ data: { demo_mode: true } });
    render(<DemoBanner />);
    const banner = screen.getByTestId("demo-banner");
    expect(banner.textContent).toContain("Demo environment");
    expect(banner.textContent).toContain("periodically reset");
  });

  it("can be dismissed for the rest of the session", () => {
    cfg.usePublicConfig.mockReturnValue({ data: { demo_mode: true } });
    render(<DemoBanner />);
    fireEvent.click(
      screen.getByRole("button", { name: "Dismiss demo notice" }),
    );
    expect(screen.queryByTestId("demo-banner")).toBeNull();
    expect(window.sessionStorage.getItem(DEMO_BANNER_DISMISSED_KEY)).toBe("1");
    cleanup();
    render(<DemoBanner />);
    expect(screen.queryByTestId("demo-banner")).toBeNull();
  });
});
