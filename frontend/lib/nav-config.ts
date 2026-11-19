import {
  LayoutDashboard,
  Plane,
  Briefcase,
  Receipt,
  CreditCard,
  CheckCircle,
  BarChart3,
  ShieldCheck,
  Users,
  FileSearch,
  UsersRound,
} from "lucide-react";
import type { PermissionKey, RoleKey } from "@/lib/types/identity";

export interface NavItem {
  label: string;
  href: string;
  icon: typeof LayoutDashboard;
  /** Visible if user has ANY of these permissions (or if undefined, visible to all) */
  requiredPermissions?: PermissionKey[];
  /** Visible if user has ANY of these roles (fallback for role-only screens) */
  requiredRoles?: RoleKey[];
  /** Optional divider label to display above this item */
  section?: string;
}

export const NAV_ITEMS: NavItem[] = [
  {
    label: "Dashboard",
    href: "/dashboard",
    icon: LayoutDashboard,
  },
  {
    label: "Travel Planner",
    href: "/planner",
    icon: Plane,
    requiredPermissions: ["trip.create.own"],
  },
  {
    label: "Trips",
    href: "/trips",
    icon: Briefcase,
    requiredPermissions: ["trip.create.own"], // Hides from finance per spec
  },
  {
    label: "Receipts",
    href: "/receipts",
    icon: Receipt,
    requiredPermissions: ["expense.create.own"],
  },
  {
    label: "Expenses",
    href: "/expenses",
    icon: CreditCard,
    requiredPermissions: ["expense.create.own"],
  },
  {
    section: "Management",
    label: "Approvals",
    href: "/approvals",
    icon: CheckCircle,
    // Managers use approval.decide.reports, Finance has oversight
    requiredPermissions: ["approval.decide.reports"],
    requiredRoles: ["finance_admin"],
  },
  {
    // Product gap 5 (US-M3): a manager's direct reports; finance sees the org.
    // Same permission split as the server's team gate (team/router.py).
    label: "Team",
    href: "/team",
    icon: UsersRound,
    requiredPermissions: ["expense.read.reports", "expense.read.org"],
  },
  {
    label: "Analytics",
    href: "/analytics",
    icon: BarChart3,
    // Finance-only (UAT2): the page renders org analytics for finance_admin and
    // only a "finance administrators" notice for anyone else, so managers no
    // longer get a nav link to a dead end (their team view is the dashboard).
    requiredRoles: ["finance_admin"],
  },
  {
    section: "Administration",
    label: "Policy & Rules",
    href: "/policy",
    icon: ShieldCheck,
    requiredPermissions: ["policy.manage"],
  },
  {
    label: "Audit",
    href: "/audit",
    icon: FileSearch,
    requiredRoles: ["finance_admin"],
  },
  {
    label: "Users",
    href: "/users",
    icon: Users,
    requiredPermissions: ["users.read"],
  },
];

/**
 * Whether a session (permissions + role) may see/open a nav item. An item is
 * accessible if it has no gates, OR the user satisfies the permission gate, OR
 * the user satisfies the role gate. Single source of truth for BOTH the visible
 * nav and the route-level gate (C7-26).
 */
export function canAccessNavItem(
  item: NavItem,
  permissions: readonly PermissionKey[],
  role: RoleKey | null,
): boolean {
  // Ungated
  if (!item.requiredPermissions && !item.requiredRoles) {
    return true;
  }

  // Permission gate
  if (item.requiredPermissions) {
    const hasPerm = item.requiredPermissions.some((p) =>
      permissions.includes(p),
    );
    if (hasPerm) return true;
  }

  // Role gate
  if (item.requiredRoles && role) {
    const hasRole = item.requiredRoles.includes(role);
    if (hasRole) return true;
  }

  return false;
}

/**
 * Filter the full nav catalog based on session permissions and role.
 */
export function getVisibleNavItems(
  permissions: readonly PermissionKey[],
  role: RoleKey | null,
): NavItem[] {
  return NAV_ITEMS.filter((item) => canAccessNavItem(item, permissions, role));
}

/**
 * Resolve the nav item that owns a pathname: exact match or a sub-route
 * ("/expenses/123" → Expenses). Longest href wins. Null for routes not in the
 * catalog. Resolved against the FULL catalog (not the visible subset) so the
 * page title is correct even when the role cannot open the page (C7-26).
 */
export function findNavItemForPath(pathname: string | null): NavItem | null {
  if (!pathname) return null;
  // Normalise so an encoded ("/%65xpenses") or doubled-slash ("//expenses")
  // spelling of a gated route cannot slip past the gate.
  let path = pathname;
  try {
    path = decodeURIComponent(pathname);
  } catch {
    // Malformed escape — match against the raw pathname.
  }
  path = path.replace(/\/{2,}/g, "/");
  let best: NavItem | null = null;
  for (const item of NAV_ITEMS) {
    if (path === item.href || path.startsWith(`${item.href}/`)) {
      if (!best || item.href.length > best.href.length) best = item;
    }
  }
  return best;
}

/**
 * Route-level role gate (C7-26). A route owned by a nav item is reachable only
 * if that item is accessible to the session — the SAME rule that hides it from
 * the nav. Routes outside the catalog are not gated here. Display-only: the
 * server still enforces every read/write.
 */
export function canAccessPath(
  pathname: string | null,
  permissions: readonly PermissionKey[],
  role: RoleKey | null,
): boolean {
  const item = findNavItemForPath(pathname);
  if (!item) return true;
  return canAccessNavItem(item, permissions, role);
}

/**
 * Get the subset of items for the mobile bottom tab bar.
 * Max 5 items total (including Assistant which is handled by the component).
 */
export function getMobileTabItems(
  permissions: readonly PermissionKey[],
  role: RoleKey | null,
): NavItem[] {
  const visible = getVisibleNavItems(permissions, role);

  // Return a curated subset for mobile based on role
  if (role === "employee") {
    return visible.filter((i) =>
      ["/dashboard", "/planner", "/trips", "/expenses"].includes(i.href),
    );
  } else if (role === "manager") {
    return visible.filter((i) =>
      ["/dashboard", "/approvals", "/team", "/expenses"].includes(i.href),
    );
  } else if (role === "finance_admin") {
    return visible.filter((i) =>
      ["/dashboard", "/approvals", "/audit", "/analytics"].includes(i.href),
    );
  }

  // Fallback
  return visible.slice(0, 4);
}
