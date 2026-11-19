/**
 * Expense module DTO mirrors — P5-T7.
 *
 * Read-only mirrors of the REAL backend contract (reconciled to code, not docs):
 *   - backend/app/modules/expense/schemas.py
 *   - database/models/expense_report.py · database/models/expense.py
 *
 * Money fields: the backend column type is Decimal, which Pydantic v2 serializes
 * to a JSON STRING (verified live: total_amount → "1351.00"). They are typed
 * `string` here to mirror the wire exactly; formatCurrency() coerces for display.
 *
 * pre_submit_flags is an UNTYPED `dict | None` passthrough on the server — its
 * shape varies (agent-emitted `{flags:[...]}` vs seeded `{warnings:[...]}`), so
 * it is typed `unknown` here and normalized in lib/expenses/readiness.ts.
 */

import type { AmountConversion } from "@/lib/money/fx";

import type { PaginationParams, SortParams } from "./api";
import type { PolicyVerdictSummary } from "./receipts";

// ---------------------------------------------------------------------------
// Enums (mirror the backend ENUMs exactly)
// ---------------------------------------------------------------------------

export type ExpenseReportStatus =
  | "draft"
  | "submitted"
  | "approved"
  | "rejected"
  | "reimbursed";

export type ExpenseCategory =
  | "airfare"
  | "lodging"
  | "meals"
  | "ground_transport"
  | "other"
  // Product gap 2: manual-only categories (never from receipts). A mileage
  // amount is computed by the server (MIL-01); entertainment / gift carry the
  // ENT-02 / ENT-04 fields.
  | "mileage"
  | "entertainment"
  | "gift";

/** Entry-level state. */
export type ExpenseEntryStatus = "draft" | "matched" | "flagged" | "approved";

/** Provenance of an entry. `manual` is visually distinct from the auto sources. */
export type ExpenseMatchSource = "receipt" | "card_txn" | "manual" | "trip";

// ---------------------------------------------------------------------------
// Response DTOs
// ---------------------------------------------------------------------------

/** Lean list row — GET /expense-reports → Page[ReportListItem].
 *  NOTE: the list row carries NO policy/flag summary (confirmed against code). */
export interface ReportListItem {
  id: string;
  organization_id: string;
  owner_id: string;
  trip_id: string | null;
  title: string;
  status: ExpenseReportStatus;
  /** Decimal serialized as a string (e.g. "1351.00"). */
  total_amount: string;
  currency: string;
  submitted_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
  /**
   * C7R/P12: the report's approval status as visible to its owner (an
   * ApprovalStatus value; null = never sent for approval). Optional for older
   * backends; typed as string so a new value never breaks rendering.
   */
  approval_status?: string | null;
  /** Product gap 1: set once finance marked the report reimbursed (simulated). */
  reimbursed_at?: string | null;
  reimbursement_reference?: string | null;
  /**
   * Product gap 4: set when the owner withdrew a RETURNED report. The status
   * stays "draft"; a withdrawn report is closed and read-only.
   */
  withdrawn_at?: string | null;
}

/** Full detail — GET /expense-reports/{id}. Adds pre_submit_flags + version. */
export interface ExpenseReport {
  id: string;
  organization_id: string;
  owner_id: string;
  trip_id: string | null;
  title: string;
  status: ExpenseReportStatus;
  /** Decimal serialized as a string (e.g. "1351.00"). */
  total_amount: string;
  currency: string;
  submitted_at: string | null;
  /** Untyped JSONB passthrough — normalize via normalizePreSubmitFlags(). */
  pre_submit_flags: unknown;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  version: number;
  /** C7R/P12: owner-visible approval status (see ReportListItem). */
  approval_status?: string | null;
  /**
   * C7R/P12: the approver's reason, present only while the report is returned
   * (changes_requested) or rejected. Rendered verbatim, never fabricated.
   */
  approval_reason?: string | null;
  /**
   * UAT2: the owner's display name — shown in the read-only banner when someone
   * other than the owner (a manager via the team read scope) opens the report.
   */
  owner_name?: string | null;
  /** Product gap 1 — simulated reimbursement (no real money moves). */
  reimbursed_at?: string | null;
  reimbursed_by_id?: string | null;
  reimbursement_reference?: string | null;
  /** Decimal serialized as a string. */
  reimbursement_amount?: string | null;
  /** Product gap 4 — the owner withdrew this returned report (read-only). */
  withdrawn_at?: string | null;
  withdrawal_reason?: string | null;
}

/** Entry row — GET /expense-reports/{id}/entries → ExpenseEntryResponse[]. */
export interface ExpenseEntry {
  id: string;
  organization_id: string;
  report_id: string;
  trip_segment_id: string | null;
  category: ExpenseCategory;
  /** Decimal serialized as a string (e.g. "450.00"). */
  amount: string;
  currency: string;
  expense_date: string;
  vendor: string | null;
  description: string | null;
  status: ExpenseEntryStatus;
  match_source: ExpenseMatchSource | null;
  is_itemized: boolean;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  /**
   * Earliest receipt linked to this entry (null when none). Fetch its scoped,
   * short-lived image link via GET /receipts/{id}.
   */
  receipt_id?: string | null;
  /** UAT2: every receipt linked to this entry (oldest first). */
  receipt_ids?: string[];
  /**
   * UAT2 defect F: the same receipts (same order) with what a reviewer needs to
   * judge each — the machine reading, direct vs shared link, whether it covers
   * this line and whether it arrived after the last return. Absent from older
   * backends (fall back to `receipt_ids`).
   */
  receipts?: EntryReceipt[];
  /**
   * C7R UAT: the claimant's written justification for a policy exception on
   * this entry (null when none). Optional for older backends.
   */
  justification?: string | null;
  /**
   * EXP-04 (product gap 2): when the owner signed the missing-receipt affidavit
   * for this entry (null when not signed). Optional for older backends.
   */
  receipt_affidavit_at?: string | null;
  /**
   * UAT2: whether the owner may delete this entry now (server-computed — a
   * reviewed line whose return kept no snapshot is not), and why not.
   */
  deletable?: boolean;
  delete_blocked_reason?: string | null;
  /**
   * UAT2 retest B: per receipt in `receipt_ids`, whether the viewer may remove
   * it now (server-computed with DELETE /receipts' own rule). Absent from older
   * backends and approver views.
   */
  receipt_removal?: EntryReceiptRemoval[];
  /**
   * Product gap 8: the amount in its report's currency when the entry is in
   * another one (server-computed, simulated reference rate); else null.
   */
  amount_conversion?: AmountConversion | null;
  /**
   * Product gap 2: people at a meal / client entertainment INCLUDING the
   * claimant (MEAL-05 / ENT-02 per-head caps). Null when not recorded.
   */
  attendee_count?: number | null;
  /** Product gap 2: ENT-04 gift / hospitality disclosure (null when none). */
  disclosure?: EntryDisclosure | null;
  /** Product gap 2: the mileage trip incl. the server-computed figures. */
  mileage?: MileageTrip | null;
}

/** ENT-04 disclosure as stored on the entry. */
export interface EntryDisclosure {
  recipient: string | null;
  recipient_organization: string | null;
  business_purpose: string | null;
  government_official: boolean | null;
}

/** What the claimant enters for a personal-vehicle trip (no amount, no rate). */
export interface MileageTripInput {
  /** One way, decimal string with at most one decimal. */
  distance: string;
  unit: "mi" | "km";
  origin: string;
  destination: string;
  round_trip: boolean;
  /** MIL-02: normal commute distance to deduct ("0" when none), same unit. */
  commute_offset: string;
  air_impractical: boolean;
}

/** The stored trip: the input + what the server derived (Decimal strings). */
export interface MileageTrip extends MileageTripInput {
  one_way_miles: string;
  claimed_miles: string;
  commute_offset_miles: string;
  billable_miles: string;
  rate_per_mile: string;
  rate_clause: string;
}

/** POST /expense-reports/{id}/mileage — the amount is computed server-side. */
export interface MileageEntryCreateRequest extends MileageTripInput {
  expense_date: string;
  description?: string | null;
}

/** One receipt backing an entry, as a reviewer sees it (UAT2 defect F). */
export interface EntryReceipt {
  receipt_id: string;
  /** `shared`: an itemized sibling's folio split onto this line. */
  link: "direct" | "shared";
  /** The receipt's MACHINE reading — null where OCR read none (typed / guessed). */
  vendor: string | null;
  receipt_date: string | null;
  /** Decimal serialized as a string. */
  amount: string | null;
  currency: string | null;
  /** The machine-read total covers this line's claim; null = no machine total. */
  covers_line: boolean | null;
  /** Attached after the report's last return — no reviewer has seen it yet. */
  attached_after_return: boolean;
}

/** Whether one receipt can be removed from its entry now (server-computed). */
export interface EntryReceiptRemoval {
  receipt_id: string;
  removable: boolean;
  /** Refused because a reviewer has seen it: it stays as audit evidence. */
  kept_for_audit: boolean;
  removal_blocked_reason: string | null;
}

// ---------------------------------------------------------------------------
// Request DTOs / query params
// ---------------------------------------------------------------------------

/** PATCH /expense-reports/{id} body — a status transition. */
export interface ReportTransitionRequest {
  status: ExpenseReportStatus;
}

/**
 * PATCH /expenses/{id} body — partial entry edit (draft reports only). Money is
 * sent as a decimal string; blank vendor / description / justification clear
 * the field. If-Match carries the PARENT REPORT version.
 */
export interface ExpenseEntryUpdate {
  category?: ExpenseCategory;
  amount?: string;
  expense_date?: string;
  vendor?: string | null;
  description?: string | null;
  justification?: string | null;
  /** EXP-04: true signs the missing-receipt affidavit, false withdraws it. */
  receipt_affidavit?: boolean;
  /** Product gap 2 — null clears. Mileage entries never send `amount`. */
  attendee_count?: number | null;
  disclosure?: EntryDisclosure | null;
  mileage?: MileageTripInput;
}

/** POST /expense-reports body — a new, empty draft report. */
export interface ExpenseReportCreateRequest {
  title: string;
  currency: string;
  trip_id?: string | null;
}

/** POST /expense-reports/{id}/entries/move response. */
export interface MoveEntriesResponse {
  source: ExpenseReport;
  target: ExpenseReport;
  moved_entry_ids: string[];
}

/** GET /expense-reports query params (allow-listed by the backend). */
export interface ReportListParams extends PaginationParams, SortParams {
  status?: ExpenseReportStatus;
  trip_id?: string;
  currency?: string;
  /**
   * UAT2: true → only the caller's own reports. The personal hooks always send
   * it; omitted → the role's full read scope (team / org-wide).
   */
  mine?: boolean;
  /**
   * Product gap 4: withdrawn reports are hidden unless asked for — true → only
   * withdrawn, "any" → everything (server default: hidden).
   */
  withdrawn?: boolean | "any";
}

/** DELETE /expenses/{id} response (UAT2). */
export interface EntryDeleteResponse {
  report: ExpenseReport;
  deleted_entry_id: string;
  /** Receipts deleted with the entry (never-submitted report). */
  deleted_receipt_ids: string[];
  /** Receipts detached but kept for audit (report has been through approval). */
  retained_receipt_ids: string[];
}

/** POST /expenses/{id}/receipts response (UAT2): attach an existing receipt. */
export interface ReceiptLinkResponse {
  report: ExpenseReport;
  entry: ExpenseEntry;
  policy_verdict: PolicyVerdictSummary;
}

/** DELETE /receipts/{id} response (UAT2). */
export interface ReceiptDeleteResponse {
  receipt_id: string;
  /** "unshared" (UAT2 hardening): a receipt split across lines was removed
   *  from ONE line only — it still backs the others. */
  outcome: "deleted" | "deleted_with_entry" | "unshared";
  /** The entry the receipt was removed from (it stays), for "deleted" / "unshared". */
  expense_id: string | null;
  report: ExpenseReport | null;
}
