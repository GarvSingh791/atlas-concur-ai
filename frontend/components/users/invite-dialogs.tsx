"use client";

import * as React from "react";
import { AlertTriangle, Copy } from "lucide-react";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { Input } from "@/components/ui/input";
import {
  useInviteUser,
  useResendInvite,
  useRevokeInvite,
} from "@/lib/query/user-admin-hooks";
import type { RoleKey } from "@/lib/types/identity";
import type { AdminUser, InviteResponse } from "@/lib/types/shell";

import {
  Field,
  FormError,
  GRADE_HINT,
  GradeSelect,
  ManagerSelect,
  ROLE_LABELS,
  ROLE_ORDER,
  SELECT_CLASS,
  serverMessage,
} from "./user-dialogs";

/**
 * Invitations — phase 12 follow-ups (finance admins).
 *
 * "Invite user" creates the account without a password; the invitee chooses
 * one from the invitation link. When the server cannot send email (console /
 * off provider) it returns the link ONCE in the response: it is shown here
 * with a copy button and a warning, lives only in this dialog's state and is
 * dropped when the dialog closes. Resend makes a new link (older ones stop
 * working); revoke voids every open link and keeps the account inactive.
 */

/** One-time link hand-over box (only when the server returned invite_url). */
export function InviteLinkBox({ url }: { url: string }) {
  const [copied, setCopied] = React.useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="mt-3 flex flex-col gap-2" data-testid="invite-link-box">
      <div
        role="alert"
        className="flex items-start gap-2 rounded-md border border-[var(--color-warning,#b45309)]/40 bg-secondary px-3 py-2 text-sm text-foreground"
      >
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <span>
          Email isn&apos;t configured on this server, so nothing was sent. Copy
          this link and give it to the person yourself. It is shown only once,
          works once, and anyone who has it can activate the account.
        </span>
      </div>
      <label
        htmlFor="invite-link"
        className="atlas-text-body font-medium text-foreground"
      >
        Invitation link
      </label>
      <div className="flex gap-2">
        <Input
          id="invite-link"
          readOnly
          value={url}
          onFocus={(e) => e.currentTarget.select()}
          className="font-mono text-xs"
        />
        <button
          type="button"
          onClick={copy}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-input px-3 text-sm hover:bg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <Copy className="h-4 w-4" aria-hidden="true" />
          {copied ? "Copied" : "Copy link"}
        </button>
      </div>
    </div>
  );
}

function InviteSent({
  result,
  onOpenChange,
}: {
  result: InviteResponse;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title={`Invitation for ${result.user.name}`}
      description={
        result.invite_url
          ? "The account is waiting for its owner to choose a password."
          : `We emailed an invitation to ${result.user.email}.`
      }
      confirmLabel="Done"
      cancelLabel="Close"
      onConfirm={() => onOpenChange(false)}
    >
      {result.invite_url && <InviteLinkBox url={result.invite_url} />}
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Invite
// ---------------------------------------------------------------------------

export function InviteUserDialog({
  onOpenChange,
}: {
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [role, setRole] = React.useState<RoleKey>("employee");
  const [managerId, setManagerId] = React.useState("");
  const [department, setDepartment] = React.useState("");
  const [grade, setGrade] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<InviteResponse | null>(null);
  const invite = useInviteUser();
  const { showToast } = useAtlasToast();

  if (result) {
    return <InviteSent result={result} onOpenChange={onOpenChange} />;
  }

  const submit = () => {
    if (invite.isPending) return;
    if (!name.trim() || !email.trim()) {
      setError("Full name and email are required.");
      return;
    }
    setError(null);
    invite.mutate(
      {
        name: name.trim(),
        email: email.trim(),
        role,
        manager_id: managerId || null,
        department: department.trim() || null,
        ...(grade ? { grade } : {}),
      },
      {
        onSuccess: (res) => {
          if (res.invite_url) {
            setResult(res); // show the one-time link
            return;
          }
          showToast({
            variant: "success",
            title: "Invitation sent",
            description: `${res.user.name} will get an email to activate their account.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(serverMessage(err, "Couldn’t send the invitation.")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="Invite user"
      description="Creates the account and sends an invitation link. The person chooses their own password; until then the account stays inactive."
      confirmLabel={invite.isPending ? "Inviting…" : "Send invitation"}
      confirmLoading={invite.isPending}
      onConfirm={submit}
      contentClassName="sm:max-w-[480px]"
    >
      <form
        className="mt-3 flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        data-testid="invite-user-form"
      >
        <Field id="invite-name" label="Full name">
          <Input
            id="invite-name"
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </Field>
        <Field id="invite-email" label="Email">
          <Input
            id="invite-email"
            type="email"
            value={email}
            maxLength={320}
            autoComplete="off"
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field id="invite-role" label="Role">
            <select
              id="invite-role"
              className={SELECT_CLASS}
              value={role}
              onChange={(e) => setRole(e.target.value as RoleKey)}
            >
              {ROLE_ORDER.map((r) => (
                <option key={r} value={r}>
                  {ROLE_LABELS[r]}
                </option>
              ))}
            </select>
          </Field>
          <Field id="invite-department" label="Department">
            <Input
              id="invite-department"
              value={department}
              maxLength={200}
              onChange={(e) => setDepartment(e.target.value)}
            />
          </Field>
        </div>
        <Field id="invite-manager" label="Manager">
          <ManagerSelect
            id="invite-manager"
            value={managerId}
            onChange={setManagerId}
          />
        </Field>
        <Field id="invite-grade" label="Grade" hint={GRADE_HINT}>
          <GradeSelect id="invite-grade" value={grade} onChange={setGrade} />
        </Field>
        <FormError message={error} />
        <button
          type="submit"
          className="hidden"
          aria-hidden="true"
          tabIndex={-1}
        />
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// Resend / revoke
// ---------------------------------------------------------------------------

export function ResendInviteDialog({
  user,
  onOpenChange,
}: {
  user: AdminUser;
  onOpenChange: (open: boolean) => void;
}) {
  const [error, setError] = React.useState<string | null>(null);
  const [result, setResult] = React.useState<InviteResponse | null>(null);
  const resend = useResendInvite();
  const { showToast } = useAtlasToast();

  if (result) {
    return <InviteSent result={result} onOpenChange={onOpenChange} />;
  }

  const confirm = () => {
    if (resend.isPending) return;
    setError(null);
    resend.mutate(
      { id: user.id },
      {
        onSuccess: (res) => {
          if (res.invite_url) {
            setResult(res);
            return;
          }
          showToast({
            variant: "success",
            title: "Invitation sent again",
            description: `We emailed a new link to ${res.user.email}.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(serverMessage(err, "Couldn’t resend the invitation.")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title={`Resend the invitation to ${user.name}?`}
      description="A new link is created and any earlier link stops working."
      confirmLabel={resend.isPending ? "Sending…" : "Resend invitation"}
      confirmLoading={resend.isPending}
      onConfirm={confirm}
    >
      <FormError message={error} />
    </Modal>
  );
}

export function RevokeInviteDialog({
  user,
  onOpenChange,
}: {
  user: AdminUser;
  onOpenChange: (open: boolean) => void;
}) {
  const [error, setError] = React.useState<string | null>(null);
  const revoke = useRevokeInvite();
  const { showToast } = useAtlasToast();

  const confirm = () => {
    if (revoke.isPending) return;
    setError(null);
    revoke.mutate(
      { id: user.id },
      {
        onSuccess: (updated) => {
          showToast({
            variant: "success",
            title: "Invitation revoked",
            description: `${updated.name}’s invitation link no longer works.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(serverMessage(err, "Couldn’t revoke the invitation.")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      variant="destructive"
      title={`Revoke ${user.name}’s invitation?`}
      description="Every invitation link for this account stops working and the account stays inactive. You can invite them again later."
      confirmLabel={revoke.isPending ? "Revoking…" : "Revoke invitation"}
      confirmLoading={revoke.isPending}
      onConfirm={confirm}
    >
      <FormError message={error} />
    </Modal>
  );
}
