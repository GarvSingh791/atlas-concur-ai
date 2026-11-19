/**
 * Token storage — P5-T2 security decision (documented).
 *
 * STORAGE STRATEGY:
 *   Access token  → module-level variable (memory only).
 *     Rationale: short-lived (15 min TTL), never persisted, destroyed on page
 *     unload. A page reload triggers a background refresh on mount.
 *
 *   Refresh token → localStorage (key: "atlas_rt").
 *     Rationale: survives page reload; without it the user must re-login on
 *     every tab close. The refresh token alone cannot read data — it can only
 *     obtain a new short-lived access token.
 *     XSS tradeoff: any XSS on this origin can read localStorage. Mitigated by:
 *       (a) CSP headers — hardening follow-up in P6.
 *       (b) Tokens are never logged or exposed via NEXT_PUBLIC_ vars.
 *       (c) Backend access tokens expire in 15 min; blast radius is bounded.
 *     httpOnly-cookie delivery is the preferred hardening path (P6 follow-up):
 *     requires the backend to set Set-Cookie on login/refresh and the client
 *     to use credentials:"include" (CORS already allows this).
 *
 * NEVER log tokens. NEVER store in a NEXT_PUBLIC_ var.
 * This module is the single choke point for token access — nothing else reads
 * or writes localStorage directly.
 */

// ---------------------------------------------------------------------------
// Access token (memory — lost on page unload, restored on mount via refresh)
// ---------------------------------------------------------------------------

let _accessToken: string | null = null;

export function getAccessToken(): string | null {
  return _accessToken;
}

export function setAccessToken(token: string): void {
  _accessToken = token;
}

export function clearAccessToken(): void {
  _accessToken = null;
}

// ---------------------------------------------------------------------------
// Refresh token (localStorage)
// ---------------------------------------------------------------------------

const RT_KEY = "atlas_rt";

export function getRefreshToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(RT_KEY);
  } catch {
    return null;
  }
}

export function setRefreshToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(RT_KEY, token);
  } catch {
    // Storage quota exceeded or privacy mode — fail silently; session degrades
    // to memory-only (user will need to re-login on reload).
  }
}

export function clearRefreshToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(RT_KEY);
  } catch {
    // ignore
  }
}

// ---------------------------------------------------------------------------
// Composite helpers
// ---------------------------------------------------------------------------

export function storeTokens(accessToken: string, refreshToken: string): void {
  setAccessToken(accessToken);
  setRefreshToken(refreshToken);
}

export function clearAllTokens(): void {
  clearAccessToken();
  clearRefreshToken();
}

// ---------------------------------------------------------------------------
// Single-flight refresh mutex
// Ensures concurrent 401s share one refresh attempt, not N parallel ones.
// ---------------------------------------------------------------------------

let _refreshPromise: Promise<string> | null = null;

/** True while a refresh is in flight — used by the client to avoid re-entry. */
export function isRefreshing(): boolean {
  return _refreshPromise !== null;
}

/**
 * Run `refreshFn` exactly once, even if called concurrently.
 * All concurrent callers receive the same Promise.
 * On success: stores the new access token, resolves all waiters.
 * On failure: clears the promise slot and rejects all waiters.
 */
export async function singleFlightRefresh(
  refreshFn: () => Promise<string>,
): Promise<string> {
  if (_refreshPromise) return _refreshPromise;

  _refreshPromise = refreshFn().then(
    (token) => {
      _refreshPromise = null;
      return token;
    },
    (err: unknown) => {
      _refreshPromise = null;
      throw err;
    },
  );

  return _refreshPromise;
}
