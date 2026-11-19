"use client";

import * as React from "react";
import { MailWarning, X } from "lucide-react";

import { errorText, useCooldown } from "@/components/auth/form-alert";
import { useSession } from "@/lib/auth/session-provider";
import { useSendVerification } from "@/lib/query/account-hooks";

/** sessionStorage key: the banner stays dismissed for the rest of the session. */
export const VERIFY_BANNER_DISMISSED_KEY = "atlas.verifyBanner.dismissed";

function readDismissed(): boolean {
  try {
    return window.sessionStorage.getItem(VERIFY_BANNER_DISMISSED_KEY) === "1";
  } catch {
    return false;
  }
}

/**
 * "Verify your email" banner — phase 12 follow-ups. Shown to a signed-in user
 * whose email is not verified (`email_verified_at === null` from GET /me; an
 * older server that omits the field shows nothing). Unverified users keep full
 * access: this is a nudge, never a block. Hidden for the shared demo accounts
 * (they cannot receive email). Dismissible per browser session.
 */
export function VerifyEmailBanner() {
  const { user } = useSession();
  const [dismissed, setDismissed] = React.useState(true);

  React.useEffect(() => setDismissed(readDismissed()), []);

  if (
    !user ||
    user.email_verified_at !== null ||
    user.demo_protected ||
    dismissed
  ) {
    return null;
  }

  const dismiss = () => {
    try {
      window.sessionStorage.setItem(VERIFY_BANNER_DISMISSED_KEY, "1");
    } catch {
      // storage unavailable — dismiss for this render tree only
    }
    setDismissed(true);
  };

  return <VerifyEmailNotice email={user.email} onDismiss={dismiss} />;
}

/** The visible banner (owns the send mutation, mounted only when shown). */
function VerifyEmailNotice({
  email,
  onDismiss,
}: {
  email: string;
  onDismiss: () => void;
}) {
  const send = useSendVerification();
  const { coolingDown, waitSeconds } = useCooldown(send.error);

  return (
    <div
      role="status"
      data-testid="verify-email-banner"
      className="flex flex-wrap items-start gap-2 border-b border-border bg-secondary px-4 py-2 text-sm text-foreground"
    >
      <MailWarning className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <p className="flex-1 min-w-[12rem]">
        <span className="font-medium">Verify your email address.</span>{" "}
        {send.isSuccess ? (
          <span data-testid="verify-banner-sent">{send.data?.message}</span>
        ) : send.error ? (
          <span
            className="text-[var(--color-error)]"
            data-testid="verify-banner-error"
          >
            {errorText(send.error, waitSeconds)}
          </span>
        ) : (
          <span>We&apos;ll send a link to {email}.</span>
        )}
      </p>
      {!send.isSuccess && (
        <button
          type="button"
          onClick={() => send.mutate()}
          disabled={send.isPending || coolingDown}
          className="rounded px-2 py-0.5 text-sm font-medium text-primary underline-offset-4 hover:underline disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {send.isPending ? "Sending…" : "Send verification email"}
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss email verification notice"
        className="rounded p-0.5 text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
