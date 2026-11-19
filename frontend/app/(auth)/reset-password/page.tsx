"use client";

/**
 * Reset password — phase 12 follow-ups.
 *
 * Opened from the emailed link `/reset-password?token=…`. The token is read
 * from the URL and sent ONLY in the JSON body of POST /auth/reset-password.
 * New password + confirmation with live strength hints; on success the user is
 * sent to the login page (every older session has been signed out). A bad /
 * expired / used link gets the server's one generic message and a way to
 * request a new link.
 */

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { KeyRound } from "lucide-react";

import { FormAlert, useCooldown } from "@/components/auth/form-alert";
import {
  PasswordRules,
  passwordProblem,
} from "@/components/auth/password-rules";
import { AtlasButton } from "@/components/atlas/button";
import { FormField } from "@/components/atlas/form-field";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useResetPassword } from "@/lib/query/account-hooks";
import { isApiError } from "@/lib/types/api";

function ResetPasswordForm() {
  const token = useSearchParams()?.get("token") ?? "";
  const router = useRouter();
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [errors, setErrors] = React.useState<{
    password?: string;
    confirm?: string;
  }>({});
  const reset = useResetPassword();
  const { coolingDown, waitSeconds } = useCooldown(reset.error);
  const linkInvalid =
    isApiError(reset.error) && reset.error.code === "INVALID_OR_EXPIRED_TOKEN";

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (coolingDown || reset.isPending) return;
    const next: typeof errors = {};
    const problem = passwordProblem(password);
    if (problem) next.password = problem;
    if (confirm !== password) next.confirm = "The passwords don't match.";
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    reset.mutate(
      { token, new_password: password },
      {
        onSuccess: () => {
          setPassword("");
          setConfirm("");
          router.replace("/login?notice=password-reset");
        },
      },
    );
  }

  if (!token) {
    return (
      <div
        className="flex flex-col gap-3 text-sm"
        data-testid="reset-missing-token"
      >
        <p className="text-foreground">
          This reset link is incomplete. Open the link from your email again, or
          request a new one.
        </p>
        <Link
          href="/forgot-password"
          className="text-primary underline-offset-4 hover:underline"
        >
          Request a new link
        </Link>
      </div>
    );
  }

  return (
    <form
      id="reset-form"
      onSubmit={submit}
      className="flex flex-col gap-4"
      noValidate
    >
      <div className="flex flex-col gap-1">
        <FormField
          id="reset-password"
          label="New password"
          type="password"
          value={password}
          onChange={setPassword}
          required
          disabled={reset.isPending}
          error={errors.password}
        />
        <PasswordRules id="reset-password-rules" value={password} />
      </div>
      <FormField
        id="reset-confirm"
        label="Confirm new password"
        type="password"
        value={confirm}
        onChange={setConfirm}
        required
        disabled={reset.isPending}
        error={errors.confirm}
      />
      <FormAlert error={reset.error} waitSeconds={waitSeconds} />
      {linkInvalid && (
        <Link
          href="/forgot-password"
          className="self-start text-sm text-primary underline-offset-4 hover:underline"
        >
          Request a new link
        </Link>
      )}
      <AtlasButton
        type="submit"
        form="reset-form"
        variant="primary"
        size="lg"
        loading={reset.isPending}
        disabled={reset.isPending || coolingDown}
        className="w-full mt-2"
      >
        <KeyRound className="h-4 w-4" aria-hidden="true" />
        {reset.isPending ? "Saving…" : "Set new password"}
      </AtlasButton>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <div
      className="flex flex-col gap-6"
      role="main"
      aria-label="Choose a new password"
    >
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-foreground">
          Choose a new password
        </h1>
        <p className="mt-2 text-sm text-muted-foreground">
          You&apos;ll be signed out everywhere else.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium text-foreground">
            New password
          </CardTitle>
        </CardHeader>
        <CardContent>
          <React.Suspense fallback={null}>
            <ResetPasswordForm />
          </React.Suspense>
        </CardContent>
      </Card>
    </div>
  );
}
