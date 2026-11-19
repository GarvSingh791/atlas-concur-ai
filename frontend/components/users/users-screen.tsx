"use client";

import * as React from "react";
import {
  Lock,
  MailPlus,
  MailX,
  Pencil,
  Send,
  UserCheck,
  UserPlus,
  Users,
  UserX,
} from "lucide-react";

import {
  AtlasButton,
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/atlas";
import { Input } from "@/components/ui/input";
import { RowActions } from "@/components/atlas/row-actions";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useSession } from "@/lib/auth/session-provider";
import { useDebouncedValue } from "@/lib/hooks/use-debounced-value";
import {
  useAdminUsers,
  useGrades,
  useOrganizationLoginCode,
} from "@/lib/query/user-admin-hooks";
import type { RoleKey } from "@/lib/types/identity";
import type { AdminUser } from "@/lib/types/shell";
import { cn } from "@/lib/utils";

import {
  AddUserDialog,
  EditUserDialog,
  gradeLabel,
  ROLE_LABELS,
  UserStatusDialog,
} from "./user-dialogs";
import {
  InviteUserDialog,
  ResendInviteDialog,
  RevokeInviteDialog,
} from "./invite-dialogs";

/**
 * Users administration — product gap 3 (replaces the "Coming soon" page).
 *
 * Finance-admin only: the route is gated by the nav config (users.read) and the
 * server refuses every write from anyone else (403). The table is server-paged
 * and server-filtered (name/email search, role, status); every change refetches
 * — nothing is optimistic.
 */

const PAGE_SIZE = 20;

/** Phase 11 (DEMO_MODE): why a shared demo account can't be changed. */
export const DEMO_PROTECTED_REASON =
  "Shared demo account — it can’t be edited or deactivated so the demo keeps working for everyone. Create your own user to try these actions.";

function DemoLockBadge({ name }: { name: string }) {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger
          type="button"
          className="ml-2 inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-muted-foreground align-middle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label={`${name} is a protected demo account`}
          data-testid="demo-lock-badge"
        >
          <Lock className="h-3 w-3" aria-hidden="true" />
          Demo
        </TooltipTrigger>
        <TooltipContent>
          <p className="max-w-xs">{DEMO_PROTECTED_REASON}</p>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

/**
 * Phase 12 follow-ups: the account's email / invitation state for the table.
 * Absent fields (an older server) show a dash.
 */
export function verificationLabel(
  u: AdminUser,
  now: number = Date.now(),
): string {
  if (u.invite_pending) {
    const expires = u.invite_expires_at ? Date.parse(u.invite_expires_at) : NaN;
    return Number.isFinite(expires) && expires <= now
      ? "Invite expired"
      : "Invited";
  }
  if (u.awaiting_activation) return "Invite revoked";
  if (u.email_verified_at === undefined) return "—";
  return u.email_verified_at ? "Verified" : "Unverified";
}

type DialogState =
  | { kind: "add" }
  | { kind: "invite" }
  | { kind: "resend"; user: AdminUser }
  | { kind: "revoke"; user: AdminUser }
  | { kind: "edit"; user: AdminUser }
  | { kind: "status"; user: AdminUser }
  | null;

const SELECT_CLASS =
  "h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function UsersScreen() {
  const { user: me, role } = useSession();
  const [search, setSearch] = React.useState("");
  const [roleFilter, setRoleFilter] = React.useState<RoleKey | "">("");
  const [statusFilter, setStatusFilter] = React.useState<
    "active" | "inactive" | ""
  >("");
  const [page, setPage] = React.useState(1);
  const [dialog, setDialog] = React.useState<DialogState>(null);

  const q = useDebouncedValue(search, 250);
  React.useEffect(() => setPage(1), [q, roleFilter, statusFilter]);

  const isAdmin = role === "finance_admin";
  const grades = useGrades(isAdmin);
  const users = useAdminUsers(
    {
      q,
      role: roleFilter || undefined,
      status: statusFilter || undefined,
      page,
      size: PAGE_SIZE,
    },
    isAdmin,
  );
  const organization = useOrganizationLoginCode(isAdmin);
  const loginCode = organization.data?.login_code;

  if (!isAdmin) {
    return (
      <EmptyState
        icon={Users}
        heading="Finance administrators only"
        subtext="User administration is available to finance administrators."
      />
    );
  }

  const data = users.data;
  const items = data?.items ?? [];
  const filtered = Boolean(q.trim() || roleFilter || statusFilter);

  return (
    <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 p-4 sm:p-6">
      <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-col gap-1">
          <h1 className="atlas-text-display text-foreground">Users</h1>
          <p className="atlas-text-body text-muted-foreground">
            Create accounts, set roles, grades and reporting lines, and
            deactivate access.
          </p>
          {loginCode && (
            <p
              className="atlas-text-body text-muted-foreground"
              data-testid="org-login-code"
            >
              Organisation login code:{" "}
              <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">
                {loginCode}
              </code>{" "}
              <span className="text-xs">
                — users enter it under &ldquo;Signing in to a specific
                organisation?&rdquo; if their email also exists in another
                organisation.
              </span>
            </p>
          )}
        </div>
        <div className="flex gap-2">
          <AtlasButton
            variant="secondary"
            size="md"
            onClick={() => setDialog({ kind: "invite" })}
          >
            <MailPlus className="mr-2 h-4 w-4" aria-hidden="true" />
            Invite user
          </AtlasButton>
          <AtlasButton
            variant="primary"
            size="md"
            onClick={() => setDialog({ kind: "add" })}
          >
            <UserPlus className="mr-2 h-4 w-4" aria-hidden="true" />
            Add user
          </AtlasButton>
        </div>
      </header>

      <div
        className="flex flex-col gap-2 sm:flex-row sm:items-center"
        role="search"
        aria-label="Filter users"
      >
        <Input
          aria-label="Search users by name or email"
          placeholder="Search name or email…"
          value={search}
          maxLength={100}
          onChange={(e) => setSearch(e.target.value)}
          className="h-9 sm:max-w-xs"
        />
        <select
          aria-label="Filter by role"
          className={SELECT_CLASS}
          value={roleFilter}
          onChange={(e) => setRoleFilter(e.target.value as RoleKey | "")}
        >
          <option value="">All roles</option>
          {(Object.keys(ROLE_LABELS) as RoleKey[]).map((r) => (
            <option key={r} value={r}>
              {ROLE_LABELS[r]}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by status"
          className={SELECT_CLASS}
          value={statusFilter}
          onChange={(e) =>
            setStatusFilter(e.target.value as "active" | "inactive" | "")
          }
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="inactive">Inactive</option>
        </select>
      </div>

      {users.isLoading ? (
        <LoadingState rows={5} />
      ) : users.isError ? (
        <ErrorState
          heading="Couldn’t load users"
          subtext="Something went wrong. Please try again."
          onRetry={() => users.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={Users}
          heading={filtered ? "No matching users" : "No users yet"}
          subtext={
            filtered
              ? "Try a different search or filter."
              : "Add the first user to your organization."
          }
        />
      ) : (
        <div className="overflow-x-auto rounded-atlas-lg border border-border bg-[var(--surface-card)]">
          <table className="w-full min-w-[920px] text-sm" aria-label="Users">
            <thead className="bg-secondary/50 text-left text-xs uppercase tracking-wide text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Name
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Email
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Role
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Manager
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Department
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Grade
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Status
                </th>
                <th scope="col" className="px-4 py-2.5 font-medium">
                  Verification
                </th>
                <th scope="col" className="px-4 py-2.5 text-right font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((u) => {
                const isSelf = me?.id === u.id;
                const active = u.status === "active";
                const locked = u.demo_protected === true;
                // Reactivation only restores a demo account, so it stays allowed.
                const statusLocked = locked && active;
                // Phase 12 follow-ups: an invited account has no password yet —
                // resend / revoke its invitation instead of reactivating it.
                const awaiting = u.awaiting_activation === true;
                return (
                  <tr
                    key={u.id}
                    className="border-t border-border"
                    data-testid={`user-row-${u.id}`}
                  >
                    <td className="px-4 py-2.5 font-medium text-foreground">
                      {u.name}
                      {isSelf && (
                        <span className="ml-1 text-xs text-muted-foreground">
                          (you)
                        </span>
                      )}
                      {locked && <DemoLockBadge name={u.name} />}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {u.email}
                    </td>
                    <td className="px-4 py-2.5">{ROLE_LABELS[u.role]}</td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {u.manager_name ?? "—"}
                    </td>
                    <td className="px-4 py-2.5 text-muted-foreground">
                      {u.department ?? "—"}
                    </td>
                    <td
                      className="px-4 py-2.5 text-muted-foreground"
                      data-testid={`user-grade-${u.id}`}
                    >
                      {gradeLabel(u.grade ?? null, grades.data)}
                    </td>
                    <td className="px-4 py-2.5">
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-xs font-medium",
                          active
                            ? "bg-secondary text-[var(--color-success)]"
                            : "bg-secondary text-muted-foreground",
                        )}
                      >
                        {active ? "Active" : "Inactive"}
                      </span>
                    </td>
                    <td
                      className="px-4 py-2.5 text-muted-foreground"
                      data-testid={`user-verification-${u.id}`}
                    >
                      {verificationLabel(u)}
                    </td>
                    <td className="px-4 py-2.5">
                      <RowActions label={`Actions for ${u.name}`}>
                        <AtlasButton
                          variant="ghost"
                          size="sm"
                          onClick={() => setDialog({ kind: "edit", user: u })}
                          disabled={locked}
                          disabledReason={
                            locked ? DEMO_PROTECTED_REASON : undefined
                          }
                          aria-label={`Edit ${u.name}`}
                        >
                          <Pencil className="h-4 w-4" aria-hidden="true" />
                        </AtlasButton>
                        {awaiting && (
                          <AtlasButton
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setDialog({ kind: "resend", user: u })
                            }
                            aria-label={`Resend invitation to ${u.name}`}
                          >
                            <Send className="h-4 w-4" aria-hidden="true" />
                          </AtlasButton>
                        )}
                        {awaiting && u.invite_pending && (
                          <AtlasButton
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setDialog({ kind: "revoke", user: u })
                            }
                            aria-label={`Revoke invitation for ${u.name}`}
                          >
                            <MailX className="h-4 w-4" aria-hidden="true" />
                          </AtlasButton>
                        )}
                        {!awaiting && (
                          <AtlasButton
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              setDialog({ kind: "status", user: u })
                            }
                            disabled={isSelf || statusLocked}
                            disabledReason={
                              statusLocked
                                ? DEMO_PROTECTED_REASON
                                : isSelf
                                  ? "You can’t deactivate your own account"
                                  : undefined
                            }
                            aria-label={
                              active
                                ? `Deactivate ${u.name}`
                                : `Reactivate ${u.name}`
                            }
                          >
                            {active ? (
                              <UserX className="h-4 w-4" aria-hidden="true" />
                            ) : (
                              <UserCheck
                                className="h-4 w-4"
                                aria-hidden="true"
                              />
                            )}
                          </AtlasButton>
                        )}
                      </RowActions>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {data && data.pages > 1 && (
        <nav
          className="flex items-center justify-between text-sm text-muted-foreground"
          aria-label="Users pagination"
        >
          <span>
            Page {data.page} of {data.pages} · {data.total} users
          </span>
          <div className="flex gap-2">
            <AtlasButton
              variant="secondary"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              Previous
            </AtlasButton>
            <AtlasButton
              variant="secondary"
              size="sm"
              disabled={page >= data.pages}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </AtlasButton>
          </div>
        </nav>
      )}

      {dialog?.kind === "add" && (
        <AddUserDialog onOpenChange={(o) => !o && setDialog(null)} />
      )}
      {dialog?.kind === "invite" && (
        <InviteUserDialog onOpenChange={(o) => !o && setDialog(null)} />
      )}
      {dialog?.kind === "resend" && (
        <ResendInviteDialog
          user={dialog.user}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
      {dialog?.kind === "revoke" && (
        <RevokeInviteDialog
          user={dialog.user}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
      {dialog?.kind === "edit" && (
        <EditUserDialog
          user={dialog.user}
          isSelf={me?.id === dialog.user.id}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
      {dialog?.kind === "status" && (
        <UserStatusDialog
          user={dialog.user}
          onOpenChange={(o) => !o && setDialog(null)}
        />
      )}
    </div>
  );
}
