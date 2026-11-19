"use client";

/**
 * SessionProvider — mounts QueryClientProvider and hydrates the auth store.
 *
 * Mount order (root layout):
 *   <SessionProvider>
 *     <AuthGuard>   ← (app) layout only
 *       {children}
 *     </AuthGuard>
 *   </SessionProvider>
 *
 * Hydration sequence on mount:
 *   1. Check for a stored refresh token.
 *   2. If found: call POST /auth/refresh to obtain a fresh access token.
 *   3. Call GET /me with the access token → populate auth store.
 *   4. If any step fails: set status = 'unauthenticated' (fail-closed).
 *
 * This runs once per app load. The auth guard reads the status and either
 * renders children or redirects to /login.
 */

import React, { useEffect } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { useShallow } from "zustand/react/shallow";

import { queryClient } from "@/lib/query/query-client";
import { api } from "@/lib/api/client";
import {
  clearAllTokens,
  getRefreshToken,
  setAccessToken,
} from "@/lib/auth/token-store";
import { isApiError } from "@/lib/types/api";
import { useAuthStore } from "@/stores/auth-store";
import type { MeResponse, AccessTokenResponse } from "@/lib/types/identity";

// ---------------------------------------------------------------------------
// Session hydration (not a hook — called once on mount)
// ---------------------------------------------------------------------------

async function hydrateSession(
  setSession: (user: MeResponse) => void,
  clearSession: () => void,
): Promise<void> {
  const refreshToken = getRefreshToken();

  if (!refreshToken) {
    // No stored refresh token → unauthenticated
    clearSession();
    return;
  }

  try {
    // Step 1: Refresh the access token using the stored refresh token.
    // We call the backend directly (not via api.post) to avoid the 401-retry
    // loop — at this point we have no access token yet.
    const refreshRes = await fetch(
      `${process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1"}/auth/refresh`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
        credentials: "include",
      },
    );

    if (!refreshRes.ok) {
      // C7-27: a definitive rejection (401/403 — expired/revoked/invalid) means
      // the stored refresh token is dead. Clear it too (mirrors
      // lib/query/auth-hooks.ts refreshAccessToken), otherwise every page load
      // wastes one 401 refresh on the stale token. Transient failures (5xx)
      // keep the token so the next load can retry.
      // Only clear if the stored token is still the one that was rejected —
      // another tab may have rotated in a fresh token meanwhile.
      if (
        (refreshRes.status === 401 || refreshRes.status === 403) &&
        getRefreshToken() === refreshToken
      ) {
        clearAllTokens();
      }
      clearSession();
      return;
    }

    const refreshData = (await refreshRes.json()) as AccessTokenResponse;
    setAccessToken(refreshData.access_token);

    // Step 2: Fetch the current user's profile.
    const user = await api.get<MeResponse>("/me");

    // Step 3: Hydrate the auth store + TanStack Query cache.
    queryClient.setQueryData(["me"], user);
    setSession(user);
  } catch (err) {
    // Any failure (network, expired token, /me 401) → unauthenticated
    // Fail-closed: never show protected content on uncertainty.
    if (isApiError(err) && err.status === 401) {
      clearAllTokens();
    }
    clearSession();
  }
}

// ---------------------------------------------------------------------------
// Provider component
// ---------------------------------------------------------------------------

interface SessionProviderProps {
  children: React.ReactNode;
}

export function SessionProvider({ children }: SessionProviderProps) {
  const setSession = useAuthStore((s) => s.setSession);
  const clearSession = useAuthStore((s) => s.clearSession);

  useEffect(() => {
    // Runs once on mount — hydrates session from stored refresh token.
    hydrateSession(setSession, clearSession);
  }, [setSession, clearSession]);

  return (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
}

// ---------------------------------------------------------------------------
// useSession — convenience hook for components
// ---------------------------------------------------------------------------

export function useSession() {
  // Zustand v5: a selector returning a NEW object every render makes
  // useSyncExternalStore see a changed snapshot each time → "getSnapshot should
  // be cached" → infinite render loop that crashes every authenticated screen
  // (AppShell, dashboard, analytics, policy, approvals). useShallow does a
  // shallow-equality compare so the snapshot is stable across renders.
  return useAuthStore(
    useShallow((s) => ({
      user: s.user,
      role: s.role,
      permissions: s.permissions,
      status: s.status,
      hasPermission: s.hasPermission,
      hasRole: s.hasRole,
    })),
  );
}
