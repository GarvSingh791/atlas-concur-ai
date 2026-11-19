/**
 * Atlas auth/session store — Zustand.
 *
 * Shape: { user, role, permissions, status }
 *
 * status:
 *   'loading'        — session hydration in progress (initial state)
 *   'authenticated'  — /me fetched successfully, user populated
 *   'unauthenticated'— no valid session (fail-closed: no token, /me failed,
 *                      refresh failed)
 *
 * Fail-closed rule: if session cannot be established for any reason,
 * status becomes 'unauthenticated'. Protected content is NEVER shown
 * in 'loading' or 'unauthenticated' states.
 *
 * Permissions derive from role via the static ROLE_PERMISSIONS map.
 * The server always enforces — this is for UI decisions only (nav visibility,
 * button enablement). Never use for security enforcement.
 */

import { create } from "zustand";
import type { MeResponse, PermissionKey, RoleKey } from "@/lib/types/identity";
import { ROLE_PERMISSIONS } from "@/lib/types/identity";

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

export type SessionStatus = "loading" | "authenticated" | "unauthenticated";

export interface AuthState {
  user: MeResponse | null;
  role: RoleKey | null;
  permissions: readonly PermissionKey[];
  status: SessionStatus;
}

export interface AuthActions {
  /** Hydrate the session from a successful /me response. */
  setSession: (user: MeResponse) => void;
  /** Clear session on logout or auth failure. */
  clearSession: () => void;
  /** Check if the current user has a specific permission. */
  hasPermission: (key: PermissionKey) => boolean;
  /** Check if the user has one of the given roles. */
  hasRole: (...roles: RoleKey[]) => boolean;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useAuthStore = create<AuthState & AuthActions>((set, get) => ({
  // --- Initial state (fail-closed: start loading, not authenticated) --------
  user: null,
  role: null,
  permissions: [],
  status: "loading",

  // --- Actions ---------------------------------------------------------------

  setSession: (user: MeResponse) => {
    const permissions = ROLE_PERMISSIONS[user.role] ?? [];
    set({
      user,
      role: user.role,
      permissions,
      status: "authenticated",
    });
  },

  clearSession: () => {
    set({
      user: null,
      role: null,
      permissions: [],
      status: "unauthenticated",
    });
  },

  hasPermission: (key: PermissionKey) => {
    return (get().permissions as PermissionKey[]).includes(key);
  },

  hasRole: (...roles: RoleKey[]) => {
    const current = get().role;
    return current !== null && roles.includes(current);
  },
}));

// ---------------------------------------------------------------------------
// Selectors (prefer these over raw state access in components)
// ---------------------------------------------------------------------------

export const selectUser = (s: AuthState) => s.user;
export const selectRole = (s: AuthState) => s.role;
export const selectPermissions = (s: AuthState) => s.permissions;
export const selectStatus = (s: AuthState) => s.status;
export const selectIsAuthenticated = (s: AuthState) =>
  s.status === "authenticated";
export const selectIsLoading = (s: AuthState) => s.status === "loading";
