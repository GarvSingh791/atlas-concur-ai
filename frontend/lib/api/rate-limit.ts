/**
 * HTTP 429 handling (phase 11 — backend/app/core/rate_limit.py).
 *
 * The backend answers a throttled request with 429, code RATE_LIMITED, a
 * `retry_after_seconds` envelope field and a `Retry-After` header. This module:
 *   - reads the wait from the body (preferred) or the header, bounded;
 *   - builds the friendly message the UI shows ("try again in 42 s");
 *   - broadcasts every 429 to subscribers (the global RateLimitToaster shows a
 *     countdown toast for the assistant panel, planner, receipt upload, user
 *     admin — any screen), so no screen needs its own wiring to be friendly;
 *   - offers `useRetryCountdown` for inline countdowns (login page).
 *
 * Nothing here retries: TanStack Query never retries a 4xx (lib/query/retry.ts)
 * and mutations never retry, so a 429 cannot turn into a retry storm.
 */

import * as React from "react";

import { isApiError, type ApiError } from "@/lib/types/api";

export const RATE_LIMITED_STATUS = 429;

/** Fallback when the server gave no usable wait. */
export const DEFAULT_RETRY_AFTER_SECONDS = 30;
/** Never show (or wait for) more than a day. */
export const MAX_RETRY_AFTER_SECONDS = 86_400;

function clampSeconds(value: number): number {
  return Math.min(MAX_RETRY_AFTER_SECONDS, Math.max(1, Math.ceil(value)));
}

/**
 * Seconds to wait: the envelope's `retry_after_seconds`, else a delta-seconds
 * `Retry-After` header, else the default. HTTP-date headers are ignored (the
 * backend never sends them; a client clock skew would make them misleading).
 */
export function parseRetryAfter(
  bodySeconds: unknown,
  header: string | null | undefined,
): number {
  if (typeof bodySeconds === "number" && Number.isFinite(bodySeconds)) {
    if (bodySeconds > 0) return clampSeconds(bodySeconds);
  }
  if (header && /^\s*\d+\s*$/.test(header)) {
    const n = Number(header.trim());
    if (n > 0) return clampSeconds(n);
  }
  return DEFAULT_RETRY_AFTER_SECONDS;
}

/** "42 seconds", "1 minute 5 seconds", "2 hours 3 minutes". */
export function formatWait(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  const plural = (n: number, unit: string) =>
    `${n} ${unit}${n === 1 ? "" : "s"}`;
  if (s < 60) return plural(s, "second");
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest
      ? `${plural(m, "minute")} ${plural(rest, "second")}`
      : plural(m, "minute");
  }
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return m ? `${plural(h, "hour")} ${plural(m, "minute")}` : plural(h, "hour");
}

export function rateLimitMessage(seconds: number): string {
  return `Too many requests. Please try again in ${formatWait(seconds)}.`;
}

/** A 429 from the API client (any code — RATE_LIMITED or a proxy's own). */
export function isRateLimitedError(err: unknown): err is ApiError {
  return isApiError(err) && err.status === RATE_LIMITED_STATUS;
}

// ---------------------------------------------------------------------------
// Broadcast — every 429 the client sees
// ---------------------------------------------------------------------------

export interface RateLimitEvent {
  retryAfterSeconds: number;
  message: string;
}

type Listener = (event: RateLimitEvent) => void;
const listeners = new Set<Listener>();

export function notifyRateLimited(event: RateLimitEvent): void {
  listeners.forEach((listener) => listener(event));
}

export function subscribeRateLimited(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// ---------------------------------------------------------------------------
// Countdown hook
// ---------------------------------------------------------------------------

/**
 * Seconds left of a wait that started when `seconds` was last set (pass a new
 * `startedAt` — e.g. the failing error object — to restart it). 0 when idle.
 */
export function useRetryCountdown(
  seconds: number | null | undefined,
  startedAt?: unknown,
): number {
  const [left, setLeft] = React.useState(() =>
    seconds && seconds > 0 ? Math.ceil(seconds) : 0,
  );

  React.useEffect(() => {
    if (!seconds || seconds <= 0) {
      setLeft(0);
      return;
    }
    const deadline = Date.now() + Math.ceil(seconds) * 1000;
    setLeft(Math.ceil(seconds));
    const timer = setInterval(() => {
      const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      setLeft(remaining);
      if (remaining === 0) clearInterval(timer);
    }, 250);
    return () => clearInterval(timer);
  }, [seconds, startedAt]);

  return left;
}
