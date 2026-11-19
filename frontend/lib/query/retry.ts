/**
 * Transient-failure retry policy for READ queries (C7R).
 *
 * Retry only what a retry can fix:
 *   - network failures: the API client's NetworkError (fetch rejected — no HTTP
 *     response), or a bare TypeError carrying one of the browsers' fetch
 *     transport-failure messages. Any OTHER TypeError ("Cannot read properties
 *     of undefined" in a queryFn/select) is a bug, not a blip: never retried;
 *   - gateway/availability errors 502 / 503 / 504 (cold starts, deploys,
 *     upstream blips on the hosted backend).
 * NEVER retry a 4xx: auth (401/403 — the session guard must see it at once),
 * not-found, validation and conflict errors are deterministic and retrying them
 * only delays the honest error state. Other 5xx (500) are server bugs, not blips.
 *
 * Backoff is exponential with "equal jitter" (half fixed, half random) so tabs
 * reconnecting after an outage do not retry in lock-step and stampede the API.
 */

import { isApiError, isNetworkError } from "@/lib/types/api";

/** Retries after the first failure (so up to 4 attempts in total). */
export const MAX_TRANSIENT_RETRIES = 3;

const TRANSIENT_STATUSES = new Set([502, 503, 504]);

/** Chrome/Edge "Failed to fetch", Firefox "NetworkError when attempting to
 *  fetch resource.", Safari "Load failed", Node/undici "fetch failed". */
const FETCH_FAILURE_MESSAGE =
  /^(failed to fetch|networkerror when attempting to fetch resource\.?|load failed|fetch failed|network request failed)/i;

/** TanStack Query `retry` callback. */
export function retryTransient(failureCount: number, error: unknown): boolean {
  return failureCount < MAX_TRANSIENT_RETRIES && isTransientError(error);
}

/** TanStack Query `retryDelay` — exponential backoff (500ms base, 8s cap) with
 *  equal jitter. `random` is injectable for deterministic tests. */
export function transientRetryDelay(
  attemptIndex: number,
  random: () => number = Math.random,
): number {
  const ceiling = Math.min(8_000, 500 * 2 ** attemptIndex);
  return Math.round(ceiling / 2 + (random() * ceiling) / 2);
}

/** True when an error is worth retrying (network or 502/503/504). */
export function isTransientError(error: unknown): boolean {
  if (isApiError(error)) return TRANSIENT_STATUSES.has(error.status);
  if (error instanceof DOMException && error.name === "AbortError") {
    return false; // cancelled by the caller — not a failure
  }
  if (isNetworkError(error)) return true;
  // A raw fetch() outside the API client: accept a TypeError only when its
  // message is one of the engines' transport-failure messages.
  return (
    error instanceof TypeError && FETCH_FAILURE_MESSAGE.test(error.message)
  );
}
