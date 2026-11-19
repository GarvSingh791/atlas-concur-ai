"use client";

/**
 * Verify email — phase 12 follow-ups.
 *
 * Opened from the emailed link `/verify-email?token=…`. Verification needs a
 * click (POST /auth/verify-email, token in the body): mail scanners that fetch
 * links in advance must not spend the one-time token. Works signed in or not.
 */

import * as React from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { MailCheck } from "lucide-react";

import {
  FormAlert,
  FormSuccess,
  useCooldown,
} from "@/components/auth/form-alert";
import { AtlasButton } from "@/components/atlas/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useVerifyEmail } from "@/lib/query/account-hooks";
import { useAuthStore } from "@/stores/auth-store";

function VerifyEmailForm() {
  const token = useSearchParams()?.get("token") ?? "";
  const verify = useVerifyEmail();
  const { coolingDown, waitSeconds } = useCooldown(verify.error);

  function confirm() {
    if (coolingDown || verify.isPending) return;
    verify.mutate(
      { token },
      {
        onSuccess: () => {
          // Hide the "verify your email" banner in this browser right away.
          const store = useAuthStore.getState();
          if (store.user) {
            store.setSession({
              ...store.user,
              email_verified_at: new Date().toISOString(),
            });
          }
        },
      },
    );
  }

  if (!token) {
    return (
      <p
        className="text-sm text-foreground"
        role="alert"
        data-testid="verify-missing-token"
      >
        This verification link is incomplete. Open the link from your email
        again.
      </p>
    );
  }
  if (verify.isSuccess) {
    return (
      <div className="flex flex-col gap-4">
        <FormSuccess testId="verify-success">
          <span className="font-medium">Your email address is verified.</span>
        </FormSuccess>
        <Link
          href="/dashboard"
          className="text-sm text-primary underline-offset-4 hover:underline"
        >
          Continue to Atlas
        </Link>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Confirm that this email address belongs to you.
      </p>
      <FormAlert error={verify.error} waitSeconds={waitSeconds} />
      <AtlasButton
        type="button"
        variant="primary"
        size="lg"
        onClick={confirm}
        loading={verify.isPending}
        disabled={verify.isPending || coolingDown}
        className="w-full"
      >
        <MailCheck className="h-4 w-4" aria-hidden="true" />
        {verify.isPending ? "Verifying…" : "Verify my email"}
      </AtlasButton>
    </div>
  );
}

export default function VerifyEmailPage() {
  return (
    <div
      className="flex flex-col gap-6"
      role="main"
      aria-label="Verify your email"
    >
      <div className="text-center">
        <h1 className="text-2xl font-semibold text-foreground">
          Verify your email
        </h1>
      </div>
      <Card>
        <CardHeader>
          <CardTitle className="text-base font-medium text-foreground">
            Email verification
          </CardTitle>
        </CardHeader>
        <CardContent>
          <React.Suspense fallback={null}>
            <VerifyEmailForm />
          </React.Suspense>
        </CardContent>
      </Card>
    </div>
  );
}
