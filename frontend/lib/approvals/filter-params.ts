/**
 * Approval queue filters <-> URL query string (Product gap 6, US-F2 AC1).
 *
 * The filter state lives in the URL so a filtered view is linkable. Parsing is
 * defensive (allow-list of keys, enum + ISO-date shape checks): a hand-edited
 * URL never sends a value the server would reject for its *shape*. The server
 * still validates everything (deny-by-default) — this is UX, not security.
 */

import type {
  ApprovalDisplayStatus,
  ApprovalSubjectType,
} from "@/lib/types/approvals";

export const APPROVAL_FILTER_KEYS = [
  "current_status",
  "subject_type",
  "exception_type",
  "department",
  "submitted_from",
  "submitted_to",
] as const;

export type ApprovalFilterKey = (typeof APPROVAL_FILTER_KEYS)[number];
export type ApprovalFilters = Partial<Record<ApprovalFilterKey, string>>;

export const APPROVAL_STATUS_OPTIONS: {
  value: ApprovalDisplayStatus;
  label: string;
}[] = [
  { value: "pending", label: "Pending" },
  { value: "changes_requested", label: "Changes requested" },
  // QA round: a returned report its owner withdrew (closed, not awaiting fixes).
  { value: "withdrawn", label: "Withdrawn" },
  { value: "approved", label: "Approved" },
  { value: "auto_approved", label: "Auto-approved" },
  { value: "rejected", label: "Rejected" },
];

export const SUBJECT_TYPE_OPTIONS: {
  value: ApprovalSubjectType;
  label: string;
}[] = [
  { value: "expense_report", label: "Expense reports" },
  { value: "trip", label: "Trips" },
];

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const MAX_LEN = 120;

function validValue(key: ApprovalFilterKey, value: string): boolean {
  if (!value || value.length > MAX_LEN) return false;
  switch (key) {
    case "current_status":
      return APPROVAL_STATUS_OPTIONS.some((o) => o.value === value);
    case "subject_type":
      return SUBJECT_TYPE_OPTIONS.some((o) => o.value === value);
    case "exception_type":
      return /^[a-z_]+$/.test(value);
    case "submitted_from":
    case "submitted_to":
      return ISO_DATE.test(value);
    default:
      return true;
  }
}

interface SearchParamsLike {
  get(name: string): string | null;
}

/** Read the allow-listed, shape-valid filters out of the URL. */
export function filtersFromSearchParams(
  params: SearchParamsLike | null,
): ApprovalFilters {
  const out: ApprovalFilters = {};
  if (!params) return out;
  for (const key of APPROVAL_FILTER_KEYS) {
    const raw = params.get(key);
    const value = raw?.trim() ?? "";
    if (validValue(key, value)) out[key] = value;
  }
  return out;
}

/** Stable query string (fixed key order, blanks dropped) — "" when no filter. */
export function filtersToQueryString(filters: ApprovalFilters): string {
  const sp = new URLSearchParams();
  for (const key of APPROVAL_FILTER_KEYS) {
    const value = filters[key]?.trim();
    if (value) sp.set(key, value);
  }
  return sp.toString();
}

/**
 * QA round: an inverted submitted-date range is a 400 on the server — catch it
 * in the filter bar instead (inline message, no request). ISO dates compare
 * correctly as strings. Null when the range is fine (or open-ended).
 */
export function dateRangeError(filters: ApprovalFilters): string | null {
  const from = filters.submitted_from?.trim();
  const to = filters.submitted_to?.trim();
  if (from && to && from > to) {
    return "“Submitted from” must be on or before “Submitted to”.";
  }
  return null;
}

export function activeFilterCount(filters: ApprovalFilters): number {
  return APPROVAL_FILTER_KEYS.filter((k) => Boolean(filters[k]?.trim())).length;
}

/** Return a copy with *key* set (or removed when blank). */
export function withFilter(
  filters: ApprovalFilters,
  key: ApprovalFilterKey,
  value: string,
): ApprovalFilters {
  const next: ApprovalFilters = { ...filters };
  const trimmed = value.trim();
  if (trimmed) next[key] = trimmed;
  else delete next[key];
  return next;
}
