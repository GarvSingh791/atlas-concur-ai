import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import { render, screen, cleanup } from "@testing-library/react";

let mockPathname = "/dashboard";
vi.mock("next/navigation", () => ({
  usePathname: () => mockPathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/auth/session-provider", () => ({ useSession: vi.fn() }));
vi.mock("@/lib/query/auth-hooks", () => ({
  useLogout: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock("@/lib/hooks/use-app-shell", () => ({
  useAppShell: () => ({
    sidebarMode: "expanded",
    isMobile: false,
    isSidebarOpen: false,
    toggleSidebar: vi.fn(),
    closeSidebar: vi.fn(),
    isAssistantOpen: false,
    toggleAssistant: vi.fn(),
    closeAssistant: vi.fn(),
  }),
}));
vi.mock("@/components/atlas/assistant-drawer", () => ({
  AssistantDrawer: () => null,
}));
vi.mock("@/components/atlas/ai-fab", () => ({ AIFab: () => null }));
vi.mock("@/components/atlas/mobile-tab-bar", () => ({
  MobileTabBar: () => null,
}));
// Product gap 3: the top bar now hosts data-fetching widgets (search, bell);
// this suite tests the route gate only, so they are stubbed like the drawer.
vi.mock("@/components/atlas/global-search", () => ({
  GlobalSearch: () => null,
}));
// Phase 11: the demo banner fetches GET /public/config (react-query); stubbed
// like the other query-backed shell children.
vi.mock("@/components/atlas/demo-banner", () => ({ DemoBanner: () => null }));
vi.mock("@/components/atlas/notification-bell", () => ({
  NotificationBell: () => null,
}));

import { useSession } from "@/lib/auth/session-provider";
import { AppShell } from "@/components/atlas/app-shell";
import {
  NAV_ITEMS,
  canAccessPath,
  findNavItemForPath,
  getVisibleNavItems,
} from "@/lib/nav-config";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";

const mockUseSession = vi.mocked(useSession);

function sessionFor(role: RoleKey) {
  mockUseSession.mockReturnValue({
    user: {
      id: "u",
      email: `${role}@atlas-demo.internal`,
      full_name: "Test User",
      role,
    },
    role,
    permissions: ROLE_PERMISSIONS[role],
    status: "authenticated",
    hasPermission: vi.fn(),
    hasRole: vi.fn(),
  } as never);
}

function renderAt(pathname: string, role: RoleKey) {
  mockPathname = pathname;
  sessionFor(role);
  return render(
    <AppShell>
      <div>PAGE CONTENT</div>
    </AppShell>,
  );
}

beforeEach(() => {
  mockPathname = "/dashboard";
});
afterEach(cleanup);

describe("C7-26 nav-config route gate (single source of truth)", () => {
  it("every route is gated exactly like its nav visibility, for every role", () => {
    for (const role of Object.keys(ROLE_PERMISSIONS) as RoleKey[]) {
      const perms = ROLE_PERMISSIONS[role];
      const visible = new Set(
        getVisibleNavItems(perms, role).map((i) => i.href),
      );
      for (const item of NAV_ITEMS) {
        expect(canAccessPath(item.href, perms, role)).toBe(
          visible.has(item.href),
        );
        // Sub-routes inherit the gate (no bypass via /expenses/<id>).
        expect(canAccessPath(`${item.href}/abc`, perms, role)).toBe(
          visible.has(item.href),
        );
      }
    }
  });

  it("finance cannot open /expenses, /receipts, /planner, /trips", () => {
    const perms = ROLE_PERMISSIONS.finance_admin;
    for (const href of ["/expenses", "/receipts", "/planner", "/trips"]) {
      expect(canAccessPath(href, perms, "finance_admin")).toBe(false);
    }
    expect(canAccessPath("/approvals", perms, "finance_admin")).toBe(true);
  });

  it("prefix matching is segment-aware (no /expenses-foo bleed)", () => {
    expect(findNavItemForPath("/expenses-foo")).toBeNull();
    expect(findNavItemForPath("/expenses/123")?.href).toBe("/expenses");
    expect(findNavItemForPath(null)).toBeNull();
    // Encoded / doubled-slash spellings cannot bypass the gate.
    expect(findNavItemForPath("/%65xpenses")?.href).toBe("/expenses");
    expect(findNavItemForPath("//expenses/1")?.href).toBe("/expenses");
    expect(
      canAccessPath(
        "/%65xpenses",
        ROLE_PERMISSIONS.finance_admin,
        "finance_admin",
      ),
    ).toBe(false);
    expect(findNavItemForPath("/%E0%A4%A")).toBeNull();
  });

  it("null role with no permissions is denied every gated route", () => {
    expect(canAccessPath("/expenses", [], null)).toBe(false);
    expect(canAccessPath("/audit", [], null)).toBe(false);
    expect(canAccessPath("/dashboard", [], null)).toBe(true);
  });
});

describe("C7-26 AppShell", () => {
  it("finance typing /expenses sees a no-access state, not the page; title is 'Expenses'", () => {
    renderAt("/expenses", "finance_admin");
    expect(
      screen.getByText(/You don't have access to this page/i),
    ).toBeTruthy();
    expect(screen.queryByText("PAGE CONTENT")).toBeNull();
    expect(screen.getByRole("heading", { name: "Expenses" })).toBeTruthy();
  });

  it.each(["/receipts", "/planner"])("finance typing %s is blocked", (path) => {
    renderAt(path, "finance_admin");
    expect(screen.queryByText("PAGE CONTENT")).toBeNull();
    expect(
      screen.getByText(/You don't have access to this page/i),
    ).toBeTruthy();
  });

  it("employee typing /audit is blocked", () => {
    renderAt("/audit", "employee");
    expect(screen.queryByText("PAGE CONTENT")).toBeNull();
  });

  it("allowed routes render the page with the right title", () => {
    renderAt("/expenses", "employee");
    expect(screen.getByText("PAGE CONTENT")).toBeTruthy();
    expect(screen.queryByText(/You don't have access/i)).toBeNull();
    cleanup();
    renderAt("/approvals", "finance_admin");
    expect(screen.getByText("PAGE CONTENT")).toBeTruthy();
  });
});
