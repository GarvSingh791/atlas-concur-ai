/**
 * Cold-start awareness for the sign-in page.
 *
 * The staging backend runs on a free instance that sleeps when idle, so the
 * first request after a quiet spell can take about a minute. This hook
 *   1. pings the backend's /health as soon as the page loads (this starts the
 *      wake-up while the visitor is still typing), and
 *   2. reports `waking` when that ping, or an in-flight sign-in, is slower than
 *      a normal response, so the page can say what is happening instead of
 *      looking broken.
 * A fast backend never shows anything. The ping is a plain GET with no
 * credentials; a failure (offline, CORS, blocked) is ignored: the sign-in
 * request itself still reports real errors.
 */
import { useEffect, useState } from "react";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

/** A response slower than this is treated as "the server is waking up". */
export const SLOW_AFTER_MS = 3000;
/** Give up waiting for the warm-up ping after this long. */
const PING_TIMEOUT_MS = 100_000;

function healthUrl(): string | null {
  try {
    return `${new URL(API_BASE).origin}/health`;
  } catch {
    return null;
  }
}

export function useServerWakeup(isSubmitting: boolean): boolean {
  const [pinging, setPinging] = useState(false);
  const [submitSlow, setSubmitSlow] = useState(false);

  useEffect(() => {
    const url = healthUrl();
    if (!url || typeof fetch !== "function") return;
    const controller = new AbortController();
    let done = false;
    const slowTimer = setTimeout(() => {
      if (!done) setPinging(true);
    }, SLOW_AFTER_MS);
    const stopTimer = setTimeout(() => controller.abort(), PING_TIMEOUT_MS);
    const settle = () => {
      done = true;
      clearTimeout(slowTimer);
      clearTimeout(stopTimer);
      setPinging(false);
    };
    try {
      fetch(url, {
        mode: "no-cors",
        cache: "no-store",
        credentials: "omit",
        signal: controller.signal,
      }).then(settle, settle);
    } catch {
      settle();
    }
    return () => {
      done = true;
      clearTimeout(slowTimer);
      clearTimeout(stopTimer);
      controller.abort();
    };
  }, []);

  useEffect(() => {
    if (!isSubmitting) {
      setSubmitSlow(false);
      return;
    }
    const t = setTimeout(() => setSubmitSlow(true), SLOW_AFTER_MS);
    return () => clearTimeout(t);
  }, [isSubmitting]);

  return pinging || submitSlow;
}
