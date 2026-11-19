"use client";

/**
 * Global "slow down" toast (phase 11). Mounted once in the root layout; shows
 * a live countdown whenever the API answers 429 — on any screen (assistant
 * panel, planner, receipt upload, user admin, ...). A newer 429 replaces the
 * running countdown; the toast closes itself when the wait is over.
 *
 * Follow-up (UI polish): routes that show their OWN inline countdown (the login
 * page) are skipped, so a throttled sign-in is reported once, not twice.
 */

import * as React from "react";

import { toast } from "@/lib/hooks/use-toast";
import { formatWait, subscribeRateLimited } from "@/lib/api/rate-limit";

const TITLE = "You're going a little fast";

/** Routes that render the 429 inline (with a countdown) — no global toast. */
export const INLINE_RATE_LIMIT_ROUTES: readonly string[] = ["/login"];

function showsInlineRateLimit(): boolean {
  if (typeof window === "undefined") return false;
  const path = window.location.pathname;
  return INLINE_RATE_LIMIT_ROUTES.some(
    (route) => path === route || path.startsWith(`${route}/`),
  );
}

function description(secondsLeft: number): string {
  return `Too many requests. Please try again in ${formatWait(secondsLeft)}.`;
}

export function RateLimitToaster(): null {
  React.useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    let handle: ReturnType<typeof toast> | null = null;

    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };

    const unsubscribe = subscribeRateLimited(({ retryAfterSeconds }) => {
      if (showsInlineRateLimit()) return;
      stop();
      const deadline = Date.now() + retryAfterSeconds * 1000;
      if (handle) handle.dismiss();
      const current = toast({
        title: TITLE,
        description: description(retryAfterSeconds),
        variant: "destructive",
      });
      handle = current;
      timer = setInterval(() => {
        const left = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        if (left === 0) {
          stop();
          current.dismiss();
          return;
        }
        current.update({
          id: current.id,
          title: TITLE,
          description: description(left),
        });
      }, 1000);
    });

    return () => {
      unsubscribe();
      stop();
    };
  }, []);

  return null;
}
