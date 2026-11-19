/**
 * Auth query hooks — useLogin, useLogout, useCurrentUser.
 *
 * These are the only resource-specific hooks built in P5-T2.
 * Domain hooks (trips, expenses, etc.) follow the pattern in query-patterns.md
 * and are built per screen in P5-T5+.
 */

"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { api } from "@/lib/api/client";
import {
  clearAllTokens,
  getRefreshToken,
  setAccessToken,
  storeTokens,
} from "@/lib/auth/token-store";
import {
  type AccessTokenResponse,
  type LoginRequest,
  type MeResponse,
  type TokenResponse,
} from "@/lib/types/identity";
import { isApiError } from "@/lib/types/api";
import { useAuthStore } from "@/stores/auth-store";

// ---------------------------------------------------------------------------
// Query keys — centralised to avoid string drift across the app
// ---------------------------------------------------------------------------

export const queryKeys = {
  me: ["me"] as const,
  users: (params?: object) => ["users", params] as const,
  // Domain keys follow this pattern — added per screen in P5-T5+
} as const;

// ---------------------------------------------------------------------------
// useCurrentUser — GET /api/v1/me
// ---------------------------------------------------------------------------

/**
 * Fetches the authenticated user's profile.
 *
 * The session provider calls this on mount to hydrate the auth store.
 * Screen components that need user data can also call this directly —
 * the cached result is shared (same query key).
 */
export function useCurrentUser() {
  return useQuery<MeResponse>({
    queryKey: queryKeys.me,
    queryFn: () => api.get<MeResponse>("/me"),
    // Don't auto-fetch — the session provider controls when this fires.
    // Set enabled:true where needed or call queryClient.fetchQuery() directly.
    enabled: false,
  });
}

// ---------------------------------------------------------------------------
// useLogin — POST /api/v1/auth/login
// ---------------------------------------------------------------------------

/**
 * Login mutation.
 *
 * On success:
 *   1. Stores access + refresh tokens.
 *   2. Fetches /me to hydrate the session.
 *   3. Redirects to /dashboard.
 *
 * On error: throws typed ApiError — caller renders the message.
 */
export function useLogin() {
  const router = useRouter();
  const qc = useQueryClient();

  return useMutation<TokenResponse, Error, LoginRequest>({
    mutationFn: (credentials) =>
      // skipAuth: a 401 here is "wrong email/password" — it must surface as an
      // ApiError for the form, not go through the refresh/redirect-to-login path.
      api.post<TokenResponse>("/auth/login", {
        body: credentials,
        skipAuth: true,
      }),
    onSuccess: async (tokens) => {
      storeTokens(tokens.access_token, tokens.refresh_token);
      // Fetch /me and ESTABLISH the session store. SessionProvider.hydrateSession
      // only runs on mount (from a stored refresh token); a client-side login
      // redirect does not re-trigger it, so the store must be set here — otherwise
      // the auth guard sees status='unauthenticated' and bounces the user back to
      // /login. Pre-populating the /me cache also avoids a second fetch.
      try {
        const user = await api.get<MeResponse>("/me");
        qc.setQueryData(queryKeys.me, user);
        useAuthStore.getState().setSession(user);
      } catch {
        // /me failed after a successful login — fail closed (stay logged out).
        useAuthStore.getState().clearSession();
      }
      router.replace("/dashboard");
    },
  });
}

// ---------------------------------------------------------------------------
// useLogout
// ---------------------------------------------------------------------------

/**
 * Clears tokens and session, redirects to login.
 * No server-side token revocation endpoint exists yet — P6 follow-up.
 */
export function useLogout() {
  const router = useRouter();
  const qc = useQueryClient();

  return () => {
    clearAllTokens();
    qc.clear();
    router.replace("/login");
  };
}

// ---------------------------------------------------------------------------
// useRefresh — POST /api/v1/auth/refresh
// (called by the session provider on mount, not by UI directly)
// ---------------------------------------------------------------------------

/**
 * Exchanges a stored refresh token for a new access token.
 * This is called by the session provider on mount — not a UI hook.
 */
export async function refreshAccessToken(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;
  try {
    const data = await api.post<AccessTokenResponse>("/auth/refresh", {
      body: { refresh_token: refreshToken },
      skipAuth: true,
    });
    setAccessToken(data.access_token);
    return data.access_token;
  } catch (err) {
    if (isApiError(err) && err.status === 401) {
      // Refresh token is expired — clear everything
      clearAllTokens();
    }
    return null;
  }
}
