"use client";

/**
 * Forgot password — phase 12 follow-ups.
 *
 * Email (+ the optional organisation login code, as on the login page). The
 * server answers 202 with the same body whether or not the account exists, so
 * this page always shows the SAME confirmation — it never says whether the
 * address is known. A 429 shows the countdown and keeps the button disabled.
 */

import * as React from "react";
import Link from "next/link";
import { Mail } from "lucide-react";

import {
  FormAlert,
  FormSuccess,
  useCooldown,
} from "@/components/auth/form-alert";
import { AtlasButton } from "@/components/atlas/button";
import { FormField } from "@/components/atlas/form-field";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getRememberedOrganization } from "@/lib/auth/org-preference";
import { useForgotPassword } from "@/lib/query/account-hooks";
import type { ForgotPasswordRequest } from "@/lib/types/identity";

export default function ForgotPasswordPage() {
  const [email, setEmail] = React.useState("");
  const [organization, setOrganization] = React.useState("");
  const [orgOpen, setOrgOpen] = React.useState(false);
  const [emailError, setEmailError] = React.useState<string | undefined>();
  const [sentTo, setSentTo] = React.useState<string | null>(null);
  const forgot = useForgotPassword();
  const { coolingDown, waitSeconds } = useCooldown(forgot.error);

  React.useEffect(() => {
    const remembered = getRememberedOrganization();
    if (remembered) {
      setOrganization(remembered);
      setOrgOpen(true);
    }
  }, []);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (coolingDown || forgot.isPending) return;
    const trimmed = email.trim();
    if (!trimmed) return setEmailError("Email is required");
    if (!/\S+@\S+\.\S+/.test(trimmed))
      return setEmailError("Enter a valid email");
    setEmailError(undefined);
    const body: ForgotPasswordRequest = { email: trimmed };
    const code = orgOpen ? organization.trim() : "";
    if (code) body.organization = code;
    forgot.mutate(body, { onSuccess: () => setSentTo(trimmed) });
  }

  return (
    <div
      className="flex flex-col gap-6"
      role="main"
      aria-label="Reset your password"
    >
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-foreground">
          Forgot your password?
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          We&apos;ll email you a link to choose a new one.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium text-foreground">
            Reset your password
          </CardTitle>
        </CardHeader>
        <CardContent>
          {sentTo !== null ? (
            <div className="flex flex-col gap-4">
              <FormSuccess testId="forgot-confirmation">
                <span className="font-medium">Check your email</span>
                <span>
                  If an account matches <strong>{sentTo}</strong>, we&apos;ve
                  sent a link to reset its password. The link works once and
                  expires soon. Didn&apos;t get it? Check your spam folder, or
                  ask your finance administrator.
                </span>
              </FormSuccess>
              <Link
                href="/login"
                className="text-sm text-primary underline-offset-4 hover:underline"
              >
                Back to sign in
              </Link>
            </div>
          ) : (
            <form
              id="forgot-form"
              onSubmit={submit}
              className="flex flex-col gap-4"
              noValidate
            >
              <FormField
                id="forgot-email"
                label="Email"
                type="email"
                value={email}
                onChange={setEmail}
                placeholder="you@company.com"
                required
                disabled={forgot.isPending}
                error={emailError}
              />
              {orgOpen ? (
                <FormField
                  id="forgot-organization"
                  label="Organisation (optional)"
                  value={organization}
                  onChange={setOrganization}
                  placeholder="e.g. atlas-demo-corp"
                  disabled={forgot.isPending}
                  helperText="Your organisation's login code, if your email is used in more than one organisation."
                />
              ) : (
                <button
                  type="button"
                  className="self-start text-xs text-primary underline-offset-4 hover:underline"
                  onClick={() => setOrgOpen(true)}
                >
                  Using a specific organisation?
                </button>
              )}
              <FormAlert error={forgot.error} waitSeconds={waitSeconds} />
              <AtlasButton
                type="submit"
                form="forgot-form"
                variant="primary"
                size="lg"
                loading={forgot.isPending}
                disabled={forgot.isPending || coolingDown}
                className="w-full mt-2"
              >
                <Mail className="h-4 w-4" aria-hidden="true" />
                {forgot.isPending ? "Sending…" : "Send reset link"}
              </AtlasButton>
              <Link
                href="/login"
                className="self-center text-sm text-muted-foreground underline-offset-4 hover:underline"
              >
                Back to sign in
              </Link>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
