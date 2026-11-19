"use client";

import * as React from "react";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { Input } from "@/components/ui/input";
import {
  useAdminUsers,
  useCreateUser,
  useGrades,
  useManagerOptions,
  useSetUserActive,
  useUpdateUser,
} from "@/lib/query/user-admin-hooks";
import { isApiError } from "@/lib/types/api";
import type { RoleKey } from "@/lib/types/identity";
import type {
  AdminUser,
  GradeOption,
  UserUpdateRequest,
} from "@/lib/types/shell";

/**
 * User administration dialogs — product gap 3.
 *
 * Mount only while open (they own their mutation hooks). Server-authoritative:
 * the server validates every field and enforces the governance rules (last
 * finance admin, no self-demotion, no manager cycles, unique email…); its
 * error-envelope message is shown inline under the form. The initial password
 * lives only in this dialog's state and is dropped when it closes.
 */

export const ROLE_LABELS: Record<RoleKey, string> = {
  employee: "Employee",
  manager: "Manager",
  finance_admin: "Finance admin",
};

export const ROLE_ORDER: RoleKey[] = ["employee", "manager", "finance_admin"];

export function serverMessage(err: unknown, fallback: string): string {
  if (isApiError(err) && err.message) return err.message;
  return fallback;
}

export const SELECT_CLASS =
  "flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

export function Field({
  id,
  label,
  hint,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={id}
        className="atlas-text-body font-medium text-foreground"
      >
        {label}
      </label>
      {children}
      {hint && (
        <p
          id={`${id}-hint`}
          className="atlas-text-caption text-muted-foreground"
        >
          {hint}
        </p>
      )}
    </div>
  );
}

export function ManagerSelect({
  id,
  value,
  onChange,
  excludeId,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  excludeId?: string;
}) {
  const managers = useManagerOptions();
  const options = (managers.data ?? []).filter((m) => m.id !== excludeId);
  return (
    <select
      id={id}
      className={SELECT_CLASS}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={managers.isLoading}
    >
      <option value="">No manager</option>
      {options.map((m) => (
        <option key={m.id} value={m.id}>
          {m.name} ({ROLE_LABELS[m.role]})
        </option>
      ))}
    </select>
  );
}

/** "Senior Vice President (SVP)" — the title, plus the short form when it differs. */
export function gradeLabel(
  code: string | null,
  grades: readonly GradeOption[] | undefined,
): string {
  if (!code) return "Not recorded";
  const g = grades?.find((x) => x.code === code);
  if (!g) return code;
  return g.short === g.title ? g.title : `${g.title} (${g.short})`;
}

export const GRADE_HINT =
  "Sets grade-based travel entitlements, e.g. business class on long flights for SVP and above (AIR-04). Not recorded = an approver decides.";

export function GradeSelect({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  const grades = useGrades();
  const options = grades.data ?? [];
  return (
    <select
      id={id}
      className={SELECT_CLASS}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || grades.isLoading}
      aria-describedby={`${id}-hint`}
    >
      <option value="">Not recorded</option>
      {/* Keep a stored code visible even before the ladder loads. */}
      {value && !options.some((g) => g.code === value) && (
        <option value={value}>{value}</option>
      )}
      {options.map((g) => (
        <option key={g.code} value={g.code}>
          {gradeLabel(g.code, options)}
        </option>
      ))}
    </select>
  );
}

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <p
      role="alert"
      className="atlas-text-caption text-[var(--color-error)]"
      data-testid="user-form-error"
    >
      {message}
    </p>
  );
}

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

export function AddUserDialog({
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
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const create = useCreateUser();
  const { showToast } = useAtlasToast();

  const submit = () => {
    if (create.isPending) return;
    if (!name.trim() || !email.trim() || !password) {
      setError("Full name, email and an initial password are required.");
      return;
    }
    setError(null);
    create.mutate(
      {
        name: name.trim(),
        email: email.trim(),
        role,
        manager_id: managerId || null,
        department: department.trim() || null,
        password,
        // Only a chosen grade is sent (the server's default is "not recorded").
        ...(grade ? { grade } : {}),
      },
      {
        onSuccess: (user) => {
          setPassword("");
          showToast({
            variant: "success",
            title: "User created",
            description: `${user.name} can now sign in with the initial password you set.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(serverMessage(err, "Couldn’t create the user.")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title="Add user"
      description="Creates an active account in your organization. Share the initial password with the user through a secure channel."
      confirmLabel={create.isPending ? "Creating…" : "Create user"}
      confirmLoading={create.isPending}
      onConfirm={submit}
      contentClassName="sm:max-w-[480px]"
    >
      <form
        className="mt-3 flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        data-testid="add-user-form"
      >
        <Field id="user-name" label="Full name">
          <Input
            id="user-name"
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </Field>
        <Field id="user-email" label="Email">
          <Input
            id="user-email"
            type="email"
            value={email}
            maxLength={320}
            autoComplete="off"
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field id="user-role" label="Role">
            <select
              id="user-role"
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
          <Field id="user-department" label="Department">
            <Input
              id="user-department"
              value={department}
              maxLength={200}
              onChange={(e) => setDepartment(e.target.value)}
            />
          </Field>
        </div>
        <Field id="user-manager" label="Manager">
          <ManagerSelect
            id="user-manager"
            value={managerId}
            onChange={setManagerId}
          />
        </Field>
        <Field id="user-grade" label="Grade" hint={GRADE_HINT}>
          <GradeSelect id="user-grade" value={grade} onChange={setGrade} />
        </Field>
        <Field
          id="user-password"
          label="Initial password"
          hint="12–72 characters, mixing at least three of: lowercase, uppercase, digits, symbols. Must not contain the email."
        >
          <Input
            id="user-password"
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
            aria-describedby="user-password-hint"
          />
        </Field>
        <FormError message={error} />
        {/* Enter submits the form */}
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
// Edit
// ---------------------------------------------------------------------------

/** Shown on your own row when you are the organisation's only active finance admin. */
export const SOLE_FINANCE_ADMIN_GRADE_HINT =
  "You’re the only active finance admin, so no one can set your grade yet — nobody may set their own (segregation of duties). To resolve it, add a user with the Finance admin role, or edit an existing user and change their role to Finance admin, then ask them to set your grade.";

export function EditUserDialog({
  user,
  isSelf = false,
  onOpenChange,
}: {
  user: AdminUser;
  /** The signed-in admin's own row: the grade is read-only (segregation of duties). */
  isSelf?: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = React.useState(user.name);
  const [role, setRole] = React.useState<RoleKey>(user.role);
  const [managerId, setManagerId] = React.useState(user.manager_id ?? "");
  const [department, setDepartment] = React.useState(user.department ?? "");
  const [grade, setGrade] = React.useState(user.grade ?? "");
  const [error, setError] = React.useState<string | null>(null);
  const update = useUpdateUser();
  // Follow-up: the self-grade lock (segregation of duties) is a dead end when
  // nobody else can lift it — say why and how to resolve it.
  const financeAdmins = useAdminUsers(
    { role: "finance_admin", status: "active", size: 1 },
    isSelf,
  );
  const soleFinanceAdmin = isSelf && financeAdmins.data?.total === 1;
  const { showToast } = useAtlasToast();

  const submit = () => {
    if (update.isPending) return;
    const changes: UserUpdateRequest = {};
    if (name.trim() !== user.name) changes.name = name.trim();
    if (role !== user.role) changes.role = role;
    if ((managerId || null) !== user.manager_id)
      changes.manager_id = managerId || null;
    if ((department.trim() || null) !== user.department)
      changes.department = department.trim() || null;
    if (!isSelf && (grade || null) !== (user.grade ?? null))
      changes.grade = grade || null;
    if (Object.keys(changes).length === 0) {
      onOpenChange(false);
      return;
    }
    setError(null);
    update.mutate(
      { id: user.id, changes },
      {
        onSuccess: (updated) => {
          showToast({
            variant: "success",
            title: "User updated",
            description: `${updated.name}’s details were saved.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(serverMessage(err, "Couldn’t save changes.")),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      title={`Edit ${user.name}`}
      description={user.email}
      confirmLabel={update.isPending ? "Saving…" : "Save changes"}
      confirmLoading={update.isPending}
      onConfirm={submit}
      contentClassName="sm:max-w-[480px]"
    >
      <form
        className="mt-3 flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
        data-testid="edit-user-form"
      >
        <Field id="edit-user-name" label="Full name">
          <Input
            id="edit-user-name"
            value={name}
            maxLength={200}
            onChange={(e) => setName(e.target.value)}
            autoFocus
          />
        </Field>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field id="edit-user-role" label="Role">
            <select
              id="edit-user-role"
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
          <Field id="edit-user-department" label="Department">
            <Input
              id="edit-user-department"
              value={department}
              maxLength={200}
              onChange={(e) => setDepartment(e.target.value)}
            />
          </Field>
        </div>
        <Field id="edit-user-manager" label="Manager">
          <ManagerSelect
            id="edit-user-manager"
            value={managerId}
            onChange={setManagerId}
            excludeId={user.id}
          />
        </Field>
        <Field
          id="edit-user-grade"
          label="Grade"
          hint={
            soleFinanceAdmin
              ? SOLE_FINANCE_ADMIN_GRADE_HINT
              : isSelf
                ? "You can’t change your own grade — another finance admin must."
                : GRADE_HINT
          }
        >
          <GradeSelect
            id="edit-user-grade"
            value={grade}
            onChange={setGrade}
            disabled={isSelf}
          />
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
// Deactivate / reactivate
// ---------------------------------------------------------------------------

export function UserStatusDialog({
  user,
  onOpenChange,
}: {
  user: AdminUser;
  onOpenChange: (open: boolean) => void;
}) {
  const activate = user.status !== "active";
  const [error, setError] = React.useState<string | null>(null);
  const setActive = useSetUserActive();
  const { showToast } = useAtlasToast();

  const confirm = () => {
    if (setActive.isPending) return;
    setError(null);
    setActive.mutate(
      { id: user.id, active: activate },
      {
        onSuccess: (updated) => {
          showToast({
            variant: "success",
            title: activate ? "User reactivated" : "User deactivated",
            description: activate
              ? `${updated.name} can sign in again.`
              : `${updated.name} can no longer sign in; open sessions were ended.`,
          });
          onOpenChange(false);
        },
        onError: (err) =>
          setError(
            serverMessage(
              err,
              activate
                ? "Couldn’t reactivate the user."
                : "Couldn’t deactivate the user.",
            ),
          ),
      },
    );
  };

  return (
    <Modal
      open
      onOpenChange={onOpenChange}
      variant={activate ? "confirm" : "destructive"}
      title={activate ? `Reactivate ${user.name}?` : `Deactivate ${user.name}?`}
      description={
        activate
          ? "They will be able to sign in again with their existing password."
          : "They will be signed out everywhere and can no longer sign in. Their records and audit history are kept."
      }
      confirmLabel={
        setActive.isPending ? "Saving…" : activate ? "Reactivate" : "Deactivate"
      }
      confirmLoading={setActive.isPending}
      onConfirm={confirm}
    >
      <FormError message={error} />
    </Modal>
  );
}
