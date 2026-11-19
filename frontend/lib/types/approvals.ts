/**
 * Approval module DTO mirrors — P5-T8.
 *
 * Read-only mirrors of the REAL backend contract (reconciled to code, not docs):
 *   - backend/app/modules/approval/schemas.py
 *   - backend/app/modules/approval/risk.py        (server-derived risk_level)
 *   - ai_agents/agents/approval_agent/evaluate.py (risk_summary shape)
 *
 * Money fields are Decimal on the server → serialized as JSON STRINGS
 * (e.g. "1234.50"); risk_summary numbers (subject_total_usd) are JSON numbers.
 *
 * risk_summary is an UNTYPED dict on the server — the agent is a deterministic
 * router that emits structured signals (NOT a generated narrative, NO confidence
 * score). We type the known keys and tolerate extras. The UI renders these
 * fields; it NEVER scores risk or evaluates policy itself.
 */

import type { ExpenseEntry, ExpenseReport } from "./expenses";

// ---------------------------------------------------------------------------
// Enums (mirror database/models/approval*.py)
// ---------------------------------------------------------------------------

export type ApprovalSubjectType = "trip" | "expense_report";

export type ApprovalStatus =
  | "pending"
  | "auto_approved"
  | "approved"
  | "rejected"
  | "changes_requested";

/**
 * QA round: what the queue SHOWS — the approval status, or "withdrawn" for a
 * returned subject its owner withdrew (the approval itself stays
 * changes_requested; the server sets `display_status`).
 */
export type ApprovalDisplayStatus = ApprovalStatus | "withdrawn";

export type ApprovalDecisionValue =
  | "approve"
  | "reject"
  | "request_changes"
  | "auto_approve";

/** Server-derived display risk level (backend approval/risk.py). */
export type RiskLevel = "low" | "medium" | "high";

// ---------------------------------------------------------------------------
// risk_summary — the Approval Agent's structured signals (untyped on server)
// ---------------------------------------------------------------------------

/**
 * One flagged exception. For expense_report subjects the flag carries a reason +
 * `source_clause_ref` (policy citation); for trip subjects it carries the
 * per-segment `in_policy` / `policy_notes` (no clause ref).
 */
export interface RiskFlag {
  // expense_report flag fields
  entry_ref?: string | null;
  flag_type?: string | null;
  rule_id?: string | null;
  rule_type?: string | null;
  source_clause_ref?: string | null;
  reason?: string | null;
  requires_approver?: string | null;
  observed?: Record<string, unknown> | null;
  severity?: string | null; // "blocking" | "advisory"
  /** Persisted expense id the flag concerns (C7R). */
  entry_id?: string | null;
  /** True when the claimant may resolve this flag with a justification (C7R UAT). */
  justifiable?: boolean | null;
  /**
   * The claimant's written justification (C7R UAT). A justified
   * policy_violation is "advisory" for SUBMIT only — it is still a policy
   * exception for the approver.
   */
  justification?: string | null;
  // trip segment "flag" fields
  segment_id?: string | null;
  segment_type?: string | null;
  in_policy?: boolean | null;
  policy_notes?: string | null;
}

export interface RiskSummary {
  route?: "escalate" | "auto_approve";
  subject_type?: ApprovalSubjectType;
  subject_in_policy?: boolean | null;
  blocking_flag_count?: number;
  requires_approver?: boolean;
  subject_total_usd?: number | null;
  routed_to_approver_id?: string | null;
  flags?: RiskFlag[];
  /** Null when nothing could be evaluated (see `evaluation_failed`). */
  is_policy_compliant?: boolean | null;
  /** Server reasons behind the verdict (e.g. why it could not be evaluated). */
  policy_rationale?: string[] | null;
  /** C7R: the owner resubmitted and the agent re-evaluated the current contents. */
  reassessed_on_resubmission?: boolean | null;
  /** ISO timestamp of that resubmission. */
  resubmitted_at?: string | null;
  /** C7R: the re-evaluation failed — no verdict exists; a human must review. */
  evaluation_failed?: boolean | null;
  /** Tolerate any extra/legacy keys without breaking the mapper. */
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// List (GET /approvals → Page[ApprovalListItem])
// ---------------------------------------------------------------------------

export interface ApprovalListItem {
  id: string;
  organization_id: string;
  subject_type: ApprovalSubjectType;
  subject_id: string;
  submitter_id: string;
  submitter_name: string | null;
  subject_title: string | null;
  /** Decimal serialized as a string (e.g. "1234.50"). */
  subject_amount: string | null;
  currency: string | null;
  current_status: ApprovalStatus;
  /** QA round: the returned subject was withdrawn by its owner (closed). */
  subject_withdrawn?: boolean;
  /** QA round: server-derived status to show ("withdrawn" or current_status). */
  display_status?: ApprovalDisplayStatus | (string & {});
  is_auto_approved: boolean;
  /** Server-derived — the client renders it, never computes it. */
  risk_level: RiskLevel;
  /** For auto-approved rows: the auto_approve step rationale (lane inline). */
  auto_approve_rationale: string | null;
  created_at: string;
  version: number;
  /** Product gap 6: the submitter's department (absent on older servers). */
  submitter_department?: string | null;
  /** Product gap 6: canonical exception types on this row (filter values). */
  exception_types?: string[];
  /**
   * Follow-up (grades): the approver grade a flag requires (ladder code, e.g.
   * "SVP"); null / absent when the usual approver may decide.
   */
  required_approver_grade?: string | null;
}

// ---------------------------------------------------------------------------
// Detail (GET /approvals/{id} → ApprovalDetailResponse)
// ---------------------------------------------------------------------------

export interface ApprovalStep {
  id: string;
  approver_id: string | null;
  approver_name: string | null;
  step_order: number;
  decision: ApprovalDecisionValue;
  reason: string | null;
  decided_at: string | null;
  created_at: string;
}

/** Trip subject summary — mirrors backend travel/schemas.py TripResponse. */
export interface ApprovalTripSummary {
  id: string;
  organization_id: string;
  traveler_id: string;
  origin: string;
  primary_destination: string;
  start_date: string;
  end_date: string;
  purpose: string | null;
  estimated_cost: string | null;
  budget_amount: string | null;
  currency: string;
  status: string;
  recommendation_context: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  version: number;
}

/** Trip segment — mirrors backend travel/schemas.py SegmentResponse. */
export interface ApprovalTripSegment {
  id: string;
  trip_id: string;
  segment_type: string;
  supplier: string | null;
  start_datetime: string | null;
  end_datetime: string | null;
  location: string | null;
  amount: string | null;
  currency: string | null;
  in_policy: boolean | null;
  policy_notes: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface ExpenseReportContext {
  report: ExpenseReport;
  entries: ExpenseEntry[];
}

export interface TripContext {
  trip: ApprovalTripSummary;
  segments: ApprovalTripSegment[];
}

/**
 * Follow-up (grades): who may decide an approval whose flags name an approver
 * grade. Computed by the server for the signed-in viewer; the server also
 * enforces it (403 INSUFFICIENT_APPROVER_GRADE).
 */
export interface GradeRequirement {
  required_grade: string;
  required_title: string;
  required_short: string;
  clauses: string[];
  routed_to_approver_id: string | null;
  routed_to_name: string | null;
  routed_by: string | null;
  rationale: string | null;
  viewer_can_decide: boolean;
  message: string;
}

export interface ApprovalDetail {
  id: string;
  organization_id: string;
  subject_type: ApprovalSubjectType;
  subject_id: string;
  submitter_id: string;
  submitter_name: string | null;
  current_status: ApprovalStatus;
  /** QA round: the returned subject was withdrawn by its owner (closed). */
  subject_withdrawn?: boolean;
  /** QA round: server-derived status to show ("withdrawn" or current_status). */
  display_status?: ApprovalDisplayStatus | (string & {});
  is_auto_approved: boolean;
  risk_summary: RiskSummary | null;
  risk_level: RiskLevel;
  version: number;
  created_at: string;
  updated_at: string;
  steps: ApprovalStep[];
  expense_report: ExpenseReportContext | null;
  trip: TripContext | null;
  /** Follow-up (grades): absent on older servers / when no grade is required. */
  grade_requirement?: GradeRequirement | null;
}

// ---------------------------------------------------------------------------
// Request DTOs / query params
// ---------------------------------------------------------------------------

/** PATCH /approvals/{id} body. reason required for reject/request_changes. */
export interface ApprovalDecisionRequest {
  decision: Exclude<ApprovalDecisionValue, "auto_approve">;
  reason?: string | null;
}

/** GET /approvals query params (allow-listed by the backend). */
export interface ApprovalListParams {
  page?: number;
  size?: number;
  sort?: string;
  current_status?: ApprovalDisplayStatus;
  subject_type?: ApprovalSubjectType;
  is_auto_approved?: boolean;
  // Product gap 6 (US-F2 AC1) — server allow-listed filters.
  exception_type?: string;
  department?: string;
  owner_id?: string;
  /** Inclusive ISO dates (YYYY-MM-DD). */
  submitted_from?: string;
  submitted_to?: string;
  decided_from?: string;
  decided_to?: string;
  amount_min?: string;
  amount_max?: string;
}
