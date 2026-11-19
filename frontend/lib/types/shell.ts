/**
 * App-shell DTO mirrors (product gap 3) — notifications, global search and
 * user administration.
 *
 * Exact mirrors of:
 *   backend/app/modules/notifications/schemas.py
 *   backend/app/modules/search/schemas.py
 *   backend/app/modules/identity/schemas.py (UserCreateRequest / UserUpdateRequest)
 *
 * Kept in its own file (not identity.ts) so the shell track touches no shared
 * type module.
 */

import type { RoleKey, UserResponse } from "./identity";

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

export type NotificationKind =
  | "approval_pending"
  | "approval_escalated"
  | "approved"
  | "rejected"
  | "returned"
  | "reimbursed"
  | "completed";

export interface NotificationItem {
  /** Deterministic id (e.g. "decision:<step id>") — stable across fetches. */
  id: string;
  kind: NotificationKind;
  title: string;
  body: string;
  subject_type: "expense_report" | "trip" | "approval";
  subject_id: string;
  /** In-app route that opens the subject. */
  link: string;
  created_at: string;
  read: boolean;
}

export interface UnreadCountResponse {
  unread: number;
}

export interface MarkReadResponse {
  marked: number;
}

// ---------------------------------------------------------------------------
// Global search
// ---------------------------------------------------------------------------

export type SearchGroupKey =
  | "trips"
  | "reports"
  | "entries"
  | "receipts"
  | "approvals"
  | "policy_rules";

export interface SearchResult {
  id: string;
  title: string;
  subtitle: string | null;
  status: string | null;
  /** Canonical route for the subject (null when no page shows it to the caller). */
  link: string | null;
  /** The subject's approval view, for roles that cannot open `link`'s page. */
  fallback_link: string | null;
}

export interface SearchGroup {
  key: SearchGroupKey;
  label: string;
  items: SearchResult[];
  truncated: boolean;
}

export interface SearchResponse {
  query: string;
  groups: SearchGroup[];
}

// ---------------------------------------------------------------------------
// User administration (finance_admin)
// ---------------------------------------------------------------------------

/** GET /users row — UserResponse plus the manager's display name. */
export interface AdminUser extends UserResponse {
  manager_name: string | null;
}

export interface UserCreateRequest {
  email: string;
  name: string;
  role: RoleKey;
  manager_id: string | null;
  department: string | null;
  /** Admin-set initial password; never stored client-side after submit. */
  password: string;
  /** Traveller grade code; null / omitted = not recorded. */
  grade?: string | null;
}

/** POST /users/invite — the fields of POST /users without a password. */
export interface UserInviteRequest {
  email: string;
  name: string;
  role: RoleKey;
  manager_id: string | null;
  department: string | null;
  grade?: string | null;
}

/** POST /users/invite and /users/{id}/invite/resend. `invite_url` is present
 * only when the server cannot deliver email — shown once, never stored. */
export interface InviteResponse {
  user: AdminUser;
  invite_url: string | null;
  invite_expires_at: string;
  email_provider: string;
}

/** PATCH /users/{id} — only the keys present change; manager_id null clears it. */
export interface UserUpdateRequest {
  name?: string;
  role?: RoleKey;
  manager_id?: string | null;
  department?: string | null;
  /** null clears it; the server refuses a change to the admin's OWN grade. */
  grade?: string | null;
}

/** GET /grades — one rung of the traveller grade ladder, most senior first. */
export interface GradeOption {
  code: string;
  title: string;
  short: string;
  rank: number;
  /** TEP-FY26 clauses this grade is entitled under (e.g. "AIR-04"). */
  entitlements: string[];
}

export interface UserListParams {
  q?: string;
  role?: RoleKey;
  status?: "active" | "inactive";
  page?: number;
  size?: number;
}
