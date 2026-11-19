/**
 * Report & trip lifecycle predicates — product gaps 1, 4, 8.
 *
 * Pure functions over server fields only (never inferred): they decide which
 * lifecycle actions the UI OFFERS. The server re-checks every one of them
 * (owner / permission / state / segregation of duties).
 */

import type {
  ExpenseReport,
  ReportListItem,
  Trip,
  TripListItem,
} from "@/lib/types";

type ReportLike = Pick<ExpenseReport, "status" | "approval_status"> & {
  withdrawn_at?: string | null;
};

/** The owner withdrew this report (status stays "draft"; it is closed). */
export function isWithdrawnReport(r: {
  withdrawn_at?: string | null;
}): boolean {
  return Boolean(r.withdrawn_at);
}

/**
 * A reviewer sent this report back and the owner has not resolved it: a draft
 * whose approval is changes_requested, or rejected and reopened. Only such a
 * report can be withdrawn.
 */
export function canWithdrawReport(r: ReportLike): boolean {
  return (
    r.status === "draft" &&
    !isWithdrawnReport(r) &&
    (r.approval_status === "changes_requested" ||
      r.approval_status === "rejected")
  );
}

/** The status shown on badges: a withdrawn draft reads "withdrawn". */
export function displayReportStatus(
  r: Pick<ReportListItem, "status"> & { withdrawn_at?: string | null },
): string {
  return isWithdrawnReport(r) ? "withdrawn" : r.status;
}

/**
 * Finance may offer "Mark reimbursed" only on an APPROVED report it does not own
 * (segregation of duties; the server refuses both otherwise).
 */
export function canReimburse(
  r: Pick<ExpenseReport, "status" | "owner_id">,
  viewer: { id: string } | null | undefined,
  hasReimbursePermission: boolean,
): boolean {
  return (
    hasReimbursePermission &&
    r.status === "approved" &&
    Boolean(viewer?.id) &&
    viewer?.id !== r.owner_id
  );
}

/** Today's calendar date as YYYY-MM-DD in UTC — the server's completion clock. */
export function todayIsoUtc(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** An approved trip whose end date has passed (<= today, UTC) may be completed. */
export function canCompleteTrip(
  t: Pick<Trip, "status" | "end_date">,
  today: string = todayIsoUtc(),
): boolean {
  return (
    t.status === "approved" &&
    Boolean(t.end_date) &&
    t.end_date.slice(0, 10) <= today
  );
}

/** A trip a reviewer returned for changes (draft + changes_requested). */
export function canWithdrawTrip(
  t: Pick<TripListItem, "status" | "approval_status"> & {
    withdrawn_at?: string | null;
  },
): boolean {
  return (
    t.status === "draft" &&
    !t.withdrawn_at &&
    t.approval_status === "changes_requested"
  );
}
