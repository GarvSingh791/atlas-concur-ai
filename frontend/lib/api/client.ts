/**
 * Atlas typed API client.
 *
 * Responsibilities:
 *   - Base URL from NEXT_PUBLIC_API_BASE_URL (includes /api/v1)
 *   - Authorization: Bearer <access_token> injection
 *   - JSON and multipart body support
 *   - Envelope handling: success responses are bare models (no wrapper)
 *   - Typed error mapping: non-2xx → ApiError (message + code + correlationId)
 *   - 401 handling: attempt refresh once (single-flight), retry original request.
 *     On refresh failure: clear session + redirect to /login.
 *     Unauthenticated auth endpoints (login/refresh) and `skipAuth` requests are
 *     exempt: their 401 is surfaced to the caller as a typed ApiError (e.g. the
 *     login form's "Invalid credentials.") instead of reloading /login.
 *   - credentials: "include" (CORS_ALLOW_CREDENTIALS=true on backend)
 *   - 429 (phase 11): ApiError with retryAfterSeconds + a friendly message,
 *     broadcast to the countdown toast; never retried (lib/api/rate-limit.ts).
 *
 * NEVER log tokens. NEVER put tokens in NEXT_PUBLIC_ vars.
 */

import {
  API_ERROR_CODES,
  ApiError,
  NetworkError,
  type ApiErrorDetail,
  type ApiErrorEnvelope,
} from "@/lib/types/api";
import {
  notifyRateLimited,
  parseRetryAfter,
  RATE_LIMITED_STATUS,
  rateLimitMessage,
} from "@/lib/api/rate-limit";
import {
  clearAllTokens,
  getAccessToken,
  getRefreshToken,
  setAccessToken,
  singleFlightRefresh,
} from "@/lib/auth/token-store";

// ---------------------------------------------------------------------------
// Base URL — NEXT_PUBLIC_API_BASE_URL includes /api/v1
// ---------------------------------------------------------------------------

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

// ---------------------------------------------------------------------------
// Request options
// ---------------------------------------------------------------------------

export interface RequestOptions {
  /** Request body — will be JSON-serialised unless FormData. */
  body?: unknown;
  /** Query string params. */
  params?: Record<string, string | number | boolean | undefined | null>;
  /** Additional headers. */
  headers?: Record<string, string>;
  /** AbortSignal for cancellation. */
  signal?: AbortSignal;
  /**
   * If true, body is sent as FormData (multipart).
   * Caller builds the FormData and passes it as `body`.
   */
  multipart?: boolean;
  /**
   * If true, the request is unauthenticated: no Authorization header is sent
   * and a 401 is thrown as a typed ApiError WITHOUT the refresh/redirect path.
   * Use for credential-exchange endpoints (login, refresh).
   */
  skipAuth?: boolean;
}

/**
 * Unauthenticated auth endpoints. A 401 from these means "bad credentials /
 * bad refresh token", never "access token expired", so they must never trigger
 * the refresh-retry or the redirect to /login (which would wipe the login form).
 */
const UNAUTHENTICATED_PATHS: ReadonlySet<string> = new Set([
  "/auth/login",
  "/auth/refresh",
]);

// ---------------------------------------------------------------------------
// Internal: build query string
// ---------------------------------------------------------------------------

function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const url = `${API_BASE}${path}`;
  if (!params) return url;

  const qs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(
      ([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`,
    )
    .join("&");

  return qs ? `${url}?${qs}` : url;
}

// ---------------------------------------------------------------------------
// Internal: parse error envelope
// ---------------------------------------------------------------------------

async function parseApiError(res: Response): Promise<ApiError> {
  if (res.status === RATE_LIMITED_STATUS) return parseRateLimited(res);
  const correlationId = res.headers.get("X-Correlation-ID") ?? "unknown";
  try {
    const envelope = (await res.json()) as ApiErrorEnvelope;
    // Inject correlationId from header if missing in body (defensive)
    const detail = {
      ...envelope.error,
      correlation_id: envelope.error?.correlation_id ?? correlationId,
    };
    return new ApiError(detail, res.status);
  } catch {
    // Non-JSON error body
    return new ApiError(
      {
        code: "NETWORK_ERROR",
        message: `HTTP ${res.status}: ${res.statusText || "Unknown error"}`,
        correlation_id: correlationId,
      },
      res.status,
    );
  }
}

/**
 * 429 (phase 11): a typed ApiError carrying `retryAfterSeconds` and a friendly
 * message, broadcast to the global countdown toast. Never retried here.
 * Exported for the SSE transport (stream.ts), which reads its own responses.
 */
export async function parseRateLimited(res: Response): Promise<ApiError> {
  const correlationId = res.headers.get("X-Correlation-ID") ?? "unknown";
  let detail: Partial<ApiErrorDetail> = {};
  try {
    detail = ((await res.json()) as ApiErrorEnvelope).error ?? {};
  } catch {
    // Non-JSON 429 (e.g. from a proxy): fall back to the header.
  }
  const seconds = parseRetryAfter(
    detail.retry_after_seconds,
    res.headers.get("Retry-After"),
  );
  const message = rateLimitMessage(seconds);
  notifyRateLimited({ retryAfterSeconds: seconds, message });
  return new ApiError(
    {
      code: detail.code ?? API_ERROR_CODES.RATE_LIMITED,
      message,
      correlation_id: detail.correlation_id ?? correlationId,
      retry_after_seconds: seconds,
    },
    RATE_LIMITED_STATUS,
  );
}

// ---------------------------------------------------------------------------
// Internal: perform a single fetch (no retry logic)
// ---------------------------------------------------------------------------

async function rawFetch(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<Response> {
  const { body, params, headers = {}, signal, multipart, skipAuth } = opts;

  const requestHeaders: Record<string, string> = { ...headers };

  // Auth injection
  if (!skipAuth) {
    const token = getAccessToken();
    if (token) {
      requestHeaders["Authorization"] = `Bearer ${token}`;
    }
  }

  // Body serialisation
  let requestBody: BodyInit | undefined;
  if (body !== undefined) {
    if (multipart) {
      // Caller passes FormData directly; let the browser set Content-Type
      requestBody = body as FormData;
    } else {
      requestBody = JSON.stringify(body);
      requestHeaders["Content-Type"] = "application/json";
    }
  }

  try {
    return await fetch(buildUrl(path, params), {
      method,
      headers: requestHeaders,
      body: requestBody,
      signal,
      credentials: "include", // Required: CORS_ALLOW_CREDENTIALS=true
    });
  } catch (err) {
    // fetch rejects with a TypeError only when no HTTP response arrived (a
    // transport failure). Tag it so the retry policy can tell it apart from a
    // TypeError bug in app code. Aborts (DOMException) pass through untouched.
    if (err instanceof TypeError) {
      throw new NetworkError(err.message || "Network request failed", {
        cause: err,
      });
    }
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Internal: refresh the access token (called from the 401 handler)
// ---------------------------------------------------------------------------

async function doRefresh(): Promise<string> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) throw new Error("No refresh token available");

  const res = await rawFetch("POST", "/auth/refresh", {
    body: { refresh_token: refreshToken },
    skipAuth: true,
  });

  if (!res.ok) {
    throw await parseApiError(res);
  }

  const data = (await res.json()) as { access_token: string };
  setAccessToken(data.access_token);
  return data.access_token;
}

// ---------------------------------------------------------------------------
// Internal: redirect to login after session loss
// ---------------------------------------------------------------------------

export function redirectToLogin(): void {
  clearAllTokens();
  if (typeof window !== "undefined") {
    window.location.replace("/login");
  }
}

// ---------------------------------------------------------------------------
// Shared 401 recovery — used by apiFetch AND the SSE transport (stream.ts)
// ---------------------------------------------------------------------------

/**
 * Recover from a 401 on a request that was sent with `staleToken`.
 *
 *   - If another caller already rotated the access token while this request
 *     was in flight, reuse the new token instead of refreshing again.
 *   - Otherwise run ONE refresh shared by every concurrent caller
 *     (singleFlightRefresh), so N parallel 401s cost a single /auth/refresh.
 *   - If the refresh fails the session is dead: clear tokens (incl. the stale
 *     refresh token, C7-27) and send the user to /login.
 *
 * Resolves to the token to retry with, or `null` when the session expired (the
 * redirect has already been issued; the caller must surface an error and stop).
 */
export async function recoverFromUnauthorized(
  staleToken: string | null,
): Promise<string | null> {
  const current = getAccessToken();
  if (current && current !== staleToken) return current;
  try {
    return await singleFlightRefresh(doRefresh);
  } catch {
    redirectToLogin();
    return null;
  }
}

/** Message used whenever a refresh fails and the user is sent to /login. */
export const SESSION_EXPIRED_MESSAGE = "Session expired. Please sign in again.";

// ---------------------------------------------------------------------------
// Core fetch wrapper with 401-refresh-retry
// ---------------------------------------------------------------------------

async function apiFetch<T>(
  method: string,
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const sentToken = getAccessToken();
  const unauthenticated =
    opts.skipAuth === true || UNAUTHENTICATED_PATHS.has(path);
  let res = await rawFetch(method, path, {
    ...opts,
    skipAuth: unauthenticated,
  });

  // 401 handling — attempt single-flight refresh, retry once. Unauthenticated
  // requests skip this: their 401 falls through to the typed-error path below.
  if (res.status === 401 && !unauthenticated) {
    const fresh = await recoverFromUnauthorized(sentToken);
    if (fresh === null) {
      // Refresh failed → session is dead (already redirected to /login)
      throw new ApiError(
        {
          code: "UNAUTHORIZED",
          message: SESSION_EXPIRED_MESSAGE,
          correlation_id: res.headers.get("X-Correlation-ID") ?? "unknown",
        },
        401,
      );
    }

    // Retry the original request with the new access token
    res = await rawFetch(method, path, opts);

    // If still 401 after refresh, clear session and throw
    if (res.status === 401) {
      redirectToLogin();
      throw await parseApiError(res);
    }
  }

  // Non-2xx after retry → typed error
  if (!res.ok) {
    throw await parseApiError(res);
  }

  // 204 No Content
  if (res.status === 204) {
    return null as T;
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Public API — typed convenience methods
// ---------------------------------------------------------------------------

export const api = {
  get<T>(path: string, opts?: Omit<RequestOptions, "body">): Promise<T> {
    return apiFetch<T>("GET", path, opts);
  },

  post<T>(path: string, opts?: RequestOptions): Promise<T> {
    return apiFetch<T>("POST", path, opts);
  },

  patch<T>(path: string, opts?: RequestOptions): Promise<T> {
    return apiFetch<T>("PATCH", path, opts);
  },

  put<T>(path: string, opts?: RequestOptions): Promise<T> {
    return apiFetch<T>("PUT", path, opts);
  },

  delete<T>(path: string, opts?: Omit<RequestOptions, "body">): Promise<T> {
    return apiFetch<T>("DELETE", path, opts);
  },
} as const;
