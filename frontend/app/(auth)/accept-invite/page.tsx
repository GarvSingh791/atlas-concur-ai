"use client";

/**
 * Accept an invitation — phase 12 follow-ups.
 *
 * Opened from the invitation link `/accept-invite?token=…` (emailed, or handed
 * over by the finance admin when the server cannot send email). Shows the
 * organisation and the masked email from GET /auth/invite/{token}; the invitee
 * confirms their name and chooses a password (POST /auth/accept-invite, token
 * in the body). No session is created: success leads to the login page.
 */

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { UserCheck } from "lucide-react";

import { FormAlert, useCooldown } from "@/components/auth/form-alert";
import {
  PasswordRules,
  passwordProblem,
} from "@/components/auth/password-rules";
import { AtlasButton } from "@/components/atlas/button";
import { FormField } from "@/components/atlas/form-field";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useAcceptInvite, useInvitePeek } from "@/lib/query/account-hooks";
import { isRateLimitedError } from "@/lib/api/rate-limit";

function InvalidInvite({ message }: { message: string }) {
  return (
    <p
      className="text-sm text-foreground"
      data-testid="invite-invalid"
      role="alert"
    >
      {message}
    </p>
  );
}

function AcceptInviteForm() {
  const token = useSearchParams()?.get("token") ?? "";
  const router = useRouter();
  const peek = useInvitePeek(token);
  const accept = useAcceptInvite();
  const peekCooldown = useCooldown(peek.error);
  const { coolingDown, waitSeconds } = useCooldown(accept.error);
  const [name, setName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [errors, setErrors] = React.useState<{
    name?: string;
    password?: string;
    confirm?: string;
  }>({});

  React.useEffect(() => {
    if (peek.data) setName(peek.data.name);
  }, [peek.data]);

  if (!token) {
    return (
      <InvalidInvite message="This invitation link is incomplete. Open the link you were sent again." />
    );
  }
  if (peek.isLoading) {
    return (
      <p className="text-sm text-muted-foreground" role="status">
        Checking your invitation…
      </p>
    );
  }
  if (peek.isError || !peek.data) {
    if (isRateLimitedError(peek.error)) {
      return (
        <FormAlert error={peek.error} waitSeconds={peekCooldown.waitSeconds} />
      );
    }
    return (
      <InvalidInvite message="This invitation link is invalid or has expired. Ask your finance administrator to send a new one." />
    );
  }
  const invite = peek.data;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (coolingDown || accept.isPending) return;
    const next: typeof errors = {};
    if (!name.trim()) next.name = "Full name is required.";
    const problem = passwordProblem(password);
    if (problem) next.password = problem;
    if (confirm !== password) next.confirm = "The passwords don't match.";
    setErrors(next);
    if (Object.keys(next).length > 0) return;
    accept.mutate(
      { token, password, name: name.trim() },
      {
        onSuccess: () => {
          setPassword("");
          setConfirm("");
          router.replace("/login?notice=invite-accepted");
        },
      },
    );
  }

  return (
    <form
      id="accept-form"
      onSubmit={submit}
      className="flex flex-col gap-4"
      noValidate
    >
      <div
        className="rounded-md border border-border bg-secondary px-3 py-2 text-sm"
        data-testid="invite-summary"
      >
        <p className="text-foreground">
          You&apos;ve been invited to{" "}
          <strong>{invite.organization_name}</strong>.
        </p>
        <p className="text-muted-foreground">
          Account: <span className="font-mono">{invite.email_masked}</span>
        </p>
      </div>
      <FormField
        id="accept-name"
        label="Full name"
        value={name}
        onChange={setName}
        required
        disabled={accept.isPending}
        error={errors.name}
      />
      <div className="flex flex-col gap-1">
        <FormField
          id="accept-password"
          label="Choose a password"
          type="password"
          value={password}
          onChange={setPassword}
          required
          disabled={accept.isPending}
          error={errors.password}
        />
        <PasswordRules id="accept-password-rules" value={password} />
      </div>
      <FormField
        id="accept-confirm"
        label="Confirm password"
        type="password"
        value={confirm}
        onChange={setConfirm}
        required
        disabled={accept.isPending}
        error={errors.confirm}
      />
      <FormAlert error={accept.error} waitSeconds={waitSeconds} />
      <AtlasButton
        type="submit"
        form="accept-form"
        variant="primary"
        size="lg"
        loading={accept.isPending}
        disabled={accept.isPending || coolingDown}
        className="w-full mt-2"
      >
        <UserCheck className="h-4 w-4" aria-hidden="true" />
        {accept.isPending ? "Activating…" : "Activate my account"}
      </AtlasButton>
    </form>
  );
}

export default function AcceptInvitePage() {
  return (
    <div
      className="flex flex-col gap-6"
      role="main"
      aria-label="Accept your invitation"
    >
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-foreground">Join Atlas</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Choose a password to activate your account.
        </p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium text-foreground">
            Accept your invitation
          </CardTitle>
        </CardHeader>
        <CardContent>
          <React.Suspense fallback={null}>
            <AcceptInviteForm />
          </React.Suspense>
        </CardContent>
      </Card>
    </div>
  );
}
