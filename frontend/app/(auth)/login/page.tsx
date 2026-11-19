"use client";

/**
 * Login page — functional form wired to useLogin().
 *
 * Uses P5-T1 primitives:
 *   - FormField (atlas) for email + password inputs
 *   - AtlasButton with loading + disabled states
 *   - Inline error envelope display (ApiError.message + correlation ID)
 *
 * On success: useLogin redirects to /dashboard.
 * If session is already authenticated: redirect to /dashboard on mount.
 *
 * Organisation-aware login (known limits): the same email may exist in more
 * than one organisation. An optional "Organisation" field (the organisation's
 * login code, from a finance admin) sits behind a small toggle; it is sent only
 * when filled and is remembered on this device. Email + password alone still
 * works whenever the email belongs to one account.
 *
 * Phase 12 follow-ups: a "Forgot password?" link, and a one-line notice after a
 * password reset (`?notice=password-reset`) or an accepted invitation
 * (`?notice=invite-accepted`).
 */

import { useState, useEffect } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FormField } from "@/components/atlas/form-field";
import { AtlasButton } from "@/components/atlas/button";
import { useLogin } from "@/lib/query/auth-hooks";
import {
  getRememberedOrganization,
  rememberOrganization,
} from "@/lib/auth/org-preference";
import { useServerWakeup } from "@/lib/auth/use-server-wakeup";
import { usePublicConfig } from "@/lib/query/public-config-hooks";
import type { LoginRequest } from "@/lib/types/identity";
import { isApiError } from "@/lib/types/api";
import {
  formatWait,
  isRateLimitedError,
  useRetryCountdown,
} from "@/lib/api/rate-limit";
import { useAuthStore, selectStatus } from "@/stores/auth-store";
import { AlertCircle, CheckCircle2, Loader2, LogIn } from "lucide-react";

/**
 * Follow-up (UI polish): both organisation-code toggles read as links — primary
 * colour, always underlined, a visible focus ring — never grey helper text.
 */
const ORG_TOGGLE_CLASS =
  "self-start rounded-sm text-xs font-medium text-primary underline underline-offset-4 hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60";

const LOGIN_NOTICES: Record<string, string> = {
  "password-reset":
    "Your password was changed and you were signed out everywhere. Sign in with your new password.",
  "invite-accepted": "Your account is active. Sign in with your new password.",
};

function readNotice(): string | null {
  try {
    const key = new URLSearchParams(window.location.search ?? "").get("notice");
    return key ? (LOGIN_NOTICES[key] ?? null) : null;
  } catch {
    return null;
  }
}

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [organization, setOrganization] = useState("");
  const [orgOpen, setOrgOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<{
    email?: string;
    password?: string;
  }>({});

  const loginMutation = useLogin();
  const publicConfig = usePublicConfig();
  // Demo deployments publish the demo organisation's code; sending it makes the
  // server verify one password hash instead of five. Never remembered or shown.
  const demoOrgCode = publicConfig.data?.demo_mode
    ? (publicConfig.data.demo_org_code ?? "")
    : "";
  const status = useAuthStore(selectStatus);
  const router = useRouter();

  // Phase 11: a throttled sign-in (429) shows a live countdown and keeps the
  // button disabled until the wait is over — no hammering, no retry storm.
  const rateLimited = isRateLimitedError(loginMutation.error)
    ? loginMutation.error
    : null;
  const waitSeconds = useRetryCountdown(
    rateLimited?.retryAfterSeconds ?? null,
    rateLimited,
  );
  const coolingDown = rateLimited !== null && waitSeconds > 0;

  // Phase 12 follow-ups: a notice after a reset / an accepted invitation.
  useEffect(() => setNotice(readNotice()), []);

  // Restore a remembered organisation code (client-only: localStorage).
  useEffect(() => {
    const remembered = getRememberedOrganization();
    if (remembered) {
      setOrganization(remembered);
      setOrgOpen(true);
    }
  }, []);

  // If already authenticated, bounce to dashboard
  useEffect(() => {
    if (status === "authenticated") {
      router.replace("/dashboard");
    }
  }, [status, router]);

  function validate(): boolean {
    const errors: { email?: string; password?: string } = {};
    if (!email.trim()) errors.email = "Email is required";
    else if (!/\S+@\S+\.\S+/.test(email)) errors.email = "Enter a valid email";
    if (!password) errors.password = "Password is required";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (coolingDown) return;
    if (!validate()) return;
    const orgCode = orgOpen ? organization.trim() : "";
    const credentials: LoginRequest = { email: email.trim(), password };
    const sentCode = orgCode || demoOrgCode;
    if (sentCode) credentials.organization = sentCode;
    loginMutation.mutate(credentials, {
      // Remember the code that worked (or forget it when signing in without one).
      onSuccess: () => rememberOrganization(orgCode),
      // Wrong credentials: keep the typed email, clear only the password.
      onError: () => setPassword(""),
    });
  }

  // Derive typed error from mutation state
  const apiError =
    loginMutation.error && isApiError(loginMutation.error)
      ? loginMutation.error
      : null;
  const genericError =
    loginMutation.error && !isApiError(loginMutation.error)
      ? "An unexpected error occurred. Please try again."
      : null;

  const isSubmitting = loginMutation.isPending;
  const waking = useServerWakeup(isSubmitting);

  // Follow-up (UI polish): once the wait is over the throttle is no longer an
  // error — "you can try again" reads as a neutral status, not a red alert.
  const readyAgain = rateLimited !== null && !coolingDown;

  const errorText = rateLimited
    ? `Too many sign-in attempts. Please try again in ${formatWait(waitSeconds)}.`
    : apiError
      ? apiError.message
      : genericError;

  return (
    <div
      className="flex flex-col gap-6"
      role="main"
      aria-label="Sign in to Atlas"
    >
      {/* Header */}
      <div className="text-center">
        {/* The "Atlas" wordmark is rendered once by app/(auth)/layout.tsx. */}
        <h1 className="text-2xl font-semibold text-foreground">
          Sign in to Atlas
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Your AI-powered Travel &amp; Expense copilot
        </p>
      </div>

      {/* Login card */}
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium text-foreground">
            Sign in with your work account
          </CardTitle>
        </CardHeader>
        <CardContent>
          <form
            id="login-form"
            onSubmit={handleSubmit}
            className="flex flex-col gap-4"
            noValidate
          >
            {notice && (
              <div
                role="status"
                className="flex items-start gap-2 rounded-md border border-border bg-secondary px-3 py-2.5 text-sm text-foreground"
                data-testid="login-notice"
              >
                <CheckCircle2
                  className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-success)]"
                  aria-hidden="true"
                />
                <span>{notice}</span>
              </div>
            )}

            {/* Email */}
            <FormField
              id="login-email"
              label="Email"
              type="email"
              value={email}
              onChange={setEmail}
              placeholder="you@company.com"
              required
              disabled={isSubmitting}
              error={fieldErrors.email}
            />

            {/* Password */}
            <FormField
              id="login-password"
              label="Password"
              type="password"
              value={password}
              onChange={setPassword}
              placeholder="••••••••"
              required
              disabled={isSubmitting}
              error={fieldErrors.password}
            />

            <Link
              href="/forgot-password"
              className="-mt-2 self-end text-xs text-primary underline-offset-4 hover:underline"
            >
              Forgot password?
            </Link>

            {/* Organisation (optional, collapsed by default) */}
            {orgOpen ? (
              <div className="flex flex-col gap-1">
                <FormField
                  id="login-organization"
                  label="Organisation"
                  value={organization}
                  onChange={setOrganization}
                  placeholder="e.g. atlas-demo-corp"
                  disabled={isSubmitting}
                  helperText="Your organisation's login code. Ask your finance administrator if you don't know it."
                />
                <button
                  type="button"
                  data-testid="login-org-dismiss"
                  className={ORG_TOGGLE_CLASS}
                  onClick={() => {
                    setOrgOpen(false);
                    setOrganization("");
                  }}
                  disabled={isSubmitting}
                >
                  Don&apos;t use an organisation code
                </button>
              </div>
            ) : (
              <button
                type="button"
                aria-expanded={false}
                className={ORG_TOGGLE_CLASS}
                onClick={() => setOrgOpen(true)}
                disabled={isSubmitting}
              >
                Signing in to a specific organisation?
              </button>
            )}

            {/* Free-tier cold start: say what is happening instead of looking broken. */}
            {waking && (
              <div
                role="status"
                aria-live="polite"
                data-testid="login-waking"
                className="flex items-start gap-2 rounded-md border border-border bg-muted px-3 py-2.5 text-sm text-foreground"
              >
                <Loader2
                  className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-muted-foreground"
                  aria-hidden="true"
                />
                <span>
                  <span className="font-medium">
                    Waking up the demo server…
                  </span>{" "}
                  It sleeps when idle, so the first sign-in can take up to a
                  minute. Keep this page open.
                </span>
              </div>
            )}

            {/* Throttle over: a neutral status (not the error style). */}
            {readyAgain && (
              <div
                role="status"
                aria-live="polite"
                data-testid="login-ready"
                className="flex items-start gap-2 rounded-md border border-border bg-muted px-3 py-2.5 text-sm text-foreground"
              >
                <CheckCircle2
                  className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-success)]"
                  aria-hidden="true"
                />
                <span className="font-medium">
                  You can try signing in again now.
                </span>
              </div>
            )}

            {/* API error envelope (a 429 shows here ONCE, with its countdown;
                the global "going a little fast" toast skips the login route). */}
            {!readyAgain && (apiError ?? genericError) && (
              <div
                role="alert"
                aria-live="assertive"
                className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2.5 text-sm text-destructive"
              >
                <AlertCircle
                  className="mt-0.5 h-4 w-4 shrink-0"
                  aria-hidden="true"
                />
                <div className="flex flex-col gap-0.5">
                  <span className="font-medium" data-testid="login-error">
                    {errorText}
                  </span>
                  {apiError?.correlationId && (
                    <span className="text-xs text-muted-foreground">
                      Ref: {apiError.correlationId}
                    </span>
                  )}
                </div>
              </div>
            )}

            {/* Submit */}
            <AtlasButton
              id="login-submit"
              type="submit"
              form="login-form"
              variant="primary"
              size="lg"
              loading={isSubmitting}
              disabled={isSubmitting || coolingDown}
              className="w-full mt-2"
            >
              <LogIn className="h-4 w-4" aria-hidden="true" />
              {isSubmitting ? "Signing in…" : "Sign in"}
            </AtlasButton>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
