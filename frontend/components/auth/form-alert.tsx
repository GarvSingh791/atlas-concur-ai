"use client";

import * as React from "react";
import { AlertCircle, CheckCircle2 } from "lucide-react";

import {
  formatWait,
  isRateLimitedError,
  useRetryCountdown,
} from "@/lib/api/rate-limit";
import { isApiError } from "@/lib/types/api";

/**
 * Shared error handling for the account-recovery forms (phase 12 follow-ups).
 *
 * `useCooldown(error)`: a 429 from the server starts the existing countdown
 * (lib/api/rate-limit.ts); the submit button stays disabled until it ends.
 * `<FormAlert>`: the server's error-envelope message (or the countdown), with
 * the correlation id for support — the same shape as the login page.
 */

export function useCooldown(error: unknown) {
  const rateLimited = isRateLimitedError(error) ? error : null;
  const waitSeconds = useRetryCountdown(
    rateLimited?.retryAfterSeconds ?? null,
    rateLimited,
  );
  return {
    rateLimited,
    waitSeconds,
    coolingDown: rateLimited !== null && waitSeconds > 0,
  };
}

export function errorText(
  error: unknown,
  waitSeconds: number,
  fallback = "Something went wrong. Please try again.",
): string {
  if (isRateLimitedError(error)) {
    return waitSeconds > 0
      ? `Too many attempts. Please try again in ${formatWait(waitSeconds)}.`
      : "You can try again now.";
  }
  if (isApiError(error) && error.message) return error.message;
  return fallback;
}

export function FormAlert({
  error,
  waitSeconds,
  fallback,
  testId = "auth-form-error",
}: {
  error: unknown;
  waitSeconds: number;
  fallback?: string;
  testId?: string;
}) {
  if (!error) return null;
  const correlationId = isApiError(error) ? error.correlationId : null;
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-sm text-destructive"
    >
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <div className="flex flex-col gap-0.5">
        <span className="font-medium" data-testid={testId}>
          {errorText(error, waitSeconds, fallback)}
        </span>
        {correlationId && correlationId !== "unknown" && (
          <span className="text-xs text-muted-foreground">
            Ref: {correlationId}
          </span>
        )}
      </div>
    </div>
  );
}

export function FormSuccess({
  children,
  testId = "auth-form-success",
}: {
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-start gap-2 rounded-md border border-border bg-secondary px-3 py-2.5 text-sm text-foreground"
      data-testid={testId}
    >
      <CheckCircle2
        className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-success)]"
        aria-hidden="true"
      />
      <div className="flex flex-col gap-1">{children}</div>
    </div>
  );
}
