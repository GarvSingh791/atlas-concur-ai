/**
 * Product gap 5 — the "Team" nav item + route gate.
 *
 * Team is visible to (and its routes open for) the roles the server's team
 * gate admits: manager (expense.read.reports) and finance (expense.read.org).
 * Employees never get the link nor the route (/team, /team/{id}). The
 * personal pages stay personal (their hooks send mine=true — covered by
 * __tests__/uat2/my-items-scope.test.tsx).
 */
import { describe, it, expect } from "vitest";

import {
  canAccessPath,
  findNavItemForPath,
  getMobileTabItems,
  getVisibleNavItems,
} from "@/lib/nav-config";
import { ROLE_PERMISSIONS, type RoleKey } from "@/lib/types/identity";

const hrefs = (role: RoleKey) =>
  getVisibleNavItems(ROLE_PERMISSIONS[role], role).map((i) => i.href);

describe("Team nav item", () => {
  it("managers and finance see Team; employees do not", () => {
    expect(hrefs("manager")).toContain("/team");
    expect(hrefs("finance_admin")).toContain("/team");
    expect(hrefs("employee")).not.toContain("/team");
  });

  it("the route gate matches the nav, including the drill-down", () => {
    for (const path of [
      "/team",
      "/team/123e4567-e89b-12d3-a456-426614174000",
    ]) {
      expect(canAccessPath(path, ROLE_PERMISSIONS.manager, "manager")).toBe(
        true,
      );
      expect(
        canAccessPath(path, ROLE_PERMISSIONS.finance_admin, "finance_admin"),
      ).toBe(true);
      expect(canAccessPath(path, ROLE_PERMISSIONS.employee, "employee")).toBe(
        false,
      );
    }
    // Encoded / doubled-slash spellings cannot slip past the gate.
    expect(
      canAccessPath("//t%65am/x", ROLE_PERMISSIONS.employee, "employee"),
    ).toBe(false);
    expect(findNavItemForPath("/team/abc")?.label).toBe("Team");
  });

  it("the gate is permission-based, not a role fallback", () => {
    // A manager-role session WITHOUT the team permission is refused (fail closed).
    expect(canAccessPath("/team", ["expense.read.own"], "manager")).toBe(false);
    expect(canAccessPath("/team", ["expense.read.reports"], "employee")).toBe(
      true,
    );
  });

  it("manager mobile tabs carry Team next to Approvals; personal pages stay", () => {
    const tabs = getMobileTabItems(ROLE_PERMISSIONS.manager, "manager").map(
      (i) => i.href,
    );
    // Nav (sidebar) order: Dashboard, Expenses, Approvals, Team.
    expect(tabs).toEqual(["/dashboard", "/expenses", "/approvals", "/team"]);
    expect(tabs.length).toBeLessThanOrEqual(4);
    // /trips is still in the sidebar for managers (personal "My Trips").
    expect(hrefs("manager")).toEqual(
      expect.arrayContaining(["/expenses", "/trips", "/approvals"]),
    );
    expect(
      getMobileTabItems(ROLE_PERMISSIONS.employee, "employee").map(
        (i) => i.href,
      ),
    ).not.toContain("/team");
  });
});
