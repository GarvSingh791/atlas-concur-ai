/**
 * risk_summary → decision-panel view-model (P5-T8).
 *
 * Pure RESHAPING of the Approval Agent's structured risk_summary into what the
 * panel renders. It does NOT score risk, evaluate policy, or derive a risk level
 * (that is server-supplied as `risk_level`). No client-side intelligence — this
 * only rearranges server truth (PRD "the UI renders risk_summary").
 */

import type {
  ApprovalSubjectType,
  RiskFlag,
  RiskSummary,
} from "@/lib/types/approvals";

export type ExceptionSeverity = "blocking" | "advisory" | "info";

export interface ExceptionView {
  /** Stable React key. */
  ref: string;
  label: string;
  reason: string | null;
  /** Policy citation (expense flags only); null for trip segments. */
  clauseRef: string | null;
  severity: ExceptionSeverity;
  /**
   * The claimant's written justification for this exception (C7R UAT), shown
   * verbatim to the approver; null when there is none.
   */
  justification: string | null;
  /**
   * What kind of exception the server flagged (its `flag_type`, as a label);
   * null for a type this screen has no label for (the row still renders).
   */
  typeLabel?: string | null;
}

/**
 * Labels for the flag types the policy engine emits — the same wording as the
 * finance exception-type filter (backend approval/filters.py). Product gap 2:
 * late submission (EXP-01), unreconciled card lines (CARD-03), policy advisory
 * (LOD-02 allowance) are report-level or in-policy notes an approver must still
 * be able to tell apart.
 */
export const FLAG_TYPE_LABEL: Record<string, string> = {
  policy_violation: "Policy violation",
  missing_receipt: "Missing receipt",
  approval_required: "Pre-approval required",
  not_evaluated: "Not evaluated",
  receipt_mismatch: "Receipt mismatch",
  possible_duplicate: "Possible duplicate",
  currency_unverified: "Currency not verified",
  policy_advisory: "Policy advisory",
  late_submission: "Late submission",
  unreconciled_card: "Unreconciled card transactions",
  // Phase 10: a re-typed line that resembles one of a withdrawn return.
  possible_refile: "Possible re-filed claim",
};

export interface AiAnalysisView {
  route: "escalate" | "auto_approve" | null;
  inPolicy: boolean | null;
  blockingCount: number;
  requiresApprover: boolean;
  totalUsd: number | null;
  /**
   * C7R: no verdict exists (the resubmission re-evaluation failed, or the
   * server marked the subject not evaluable) — render "Not evaluated", never
   * an in/out-of-policy answer.
   */
  notEvaluated: boolean;
  /** C7R: ISO time of the owner's resubmission the verdict was re-run for. */
  reassessedAt: string | null;
  /** True when the verdict was re-computed after a resubmission. */
  reassessed: boolean;
  /** Server-written reasons behind the verdict, shown verbatim. */
  rationale: string[];
}

export interface DecisionView {
  aiAnalysis: AiAnalysisView;
  exceptions: ExceptionView[];
  /** Short summary of in-policy items (trip segment counts); null otherwise. */
  inPolicyNote: string | null;
}

const EMPTY_ANALYSIS: AiAnalysisView = {
  route: null,
  inPolicy: null,
  blockingCount: 0,
  requiresApprover: false,
  totalUsd: null,
  notEvaluated: false,
  reassessedAt: null,
  reassessed: false,
  rationale: [],
};

function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((v): v is string => typeof v === "string")
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
}

function normalizeSeverity(
  severity: string | null | undefined,
): ExceptionSeverity {
  if (severity === "blocking") return "blocking";
  if (severity === "advisory") return "advisory";
  return "info";
}

function titleCase(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function segmentLabel(flag: RiskFlag, index: number): string {
  if (flag.segment_type) return `${titleCase(flag.segment_type)} segment`;
  return `Segment ${index + 1}`;
}

/** C7R SoD: the submitter has a returned, unresolved subject (never auto-approved). */
export const OPEN_RETURNED_FLAG = "open_returned_report";
/** C7R SoD: the subject re-files content a reviewer turned down. */
export const REFILED_REJECTED_FLAG = "refiled_rejected_claim";
/**
 * Product-gaps integration: the subject matches one a reviewer sent back for
 * changes and the owner then withdrew — escalated, never auto-approved.
 */
export const REFILED_RETURNED_FLAG = "refiled_returned_claim";
/**
 * UAT-2 Issue 4: the trip overlaps another live trip of the same traveler to
 * the same destination — possibly one journey split into separate trips.
 */
export const POSSIBLE_SPLIT_TRIP_FLAG = "possible_split_trip";

/** Whole-trip blocking flags (not about one segment), shown as exceptions. */
const TRIP_SUBJECT_FLAGS = new Set<string>([
  OPEN_RETURNED_FLAG,
  REFILED_REJECTED_FLAG,
  REFILED_RETURNED_FLAG,
  POSSIBLE_SPLIT_TRIP_FLAG,
]);

const SUBJECT_FLAG_LABEL: Record<string, string> = {
  [OPEN_RETURNED_FLAG]: "Returned item",
  [REFILED_REJECTED_FLAG]: "Possible re-filed trip",
  [REFILED_RETURNED_FLAG]: "Possible re-filed withdrawn trip",
  [POSSIBLE_SPLIT_TRIP_FLAG]: "Overlapping trip",
};

function isSubjectFlag(flag: RiskFlag): boolean {
  return !!flag.flag_type && TRIP_SUBJECT_FLAGS.has(flag.flag_type);
}

function isSegmentFlag(flag: RiskFlag): boolean {
  return !isSubjectFlag(flag);
}

function openReturnExceptions(flags: RiskFlag[]): ExceptionView[] {
  return flags.filter(isSubjectFlag).map((flag, index) => ({
    ref: `${flag.flag_type}-${index}`,
    label:
      flag.entry_ref ??
      SUBJECT_FLAG_LABEL[flag.flag_type ?? ""] ??
      "Flagged trip",
    reason: flag.reason ?? null,
    clauseRef: null,
    severity: "blocking" as const,
    justification: null,
  }));
}

function tripExceptions(flags: RiskFlag[]): ExceptionView[] {
  return [...openReturnExceptions(flags), ...tripSegmentExceptions(flags)];
}

function tripSegmentExceptions(flags: RiskFlag[]): ExceptionView[] {
  return flags
    .map((flag, index) => ({ flag, index }))
    .filter(({ flag }) => isSegmentFlag(flag) && flag.in_policy === false)
    .map(({ flag, index }) => ({
      ref: flag.segment_id ?? `segment-${index}`,
      label: segmentLabel(flag, index),
      reason: flag.policy_notes ?? null,
      clauseRef: null, // trip segments carry policy_notes, not a clause ref
      severity: "advisory" as const,
      justification: null,
    }));
}

/** Non-blank justification text on a flag, else null. */
export function flagJustification(flag: RiskFlag): string | null {
  const text =
    typeof flag.justification === "string" ? flag.justification.trim() : "";
  return text.length > 0 ? text : null;
}

/**
 * C7R UAT: a policy violation is an exception for the APPROVER whatever its
 * submit severity. A claimant's justification makes it "advisory" only so the
 * report can be submitted; the server still counts it as blocking
 * (approval_agent `_is_blocking` → blocking_flag_count), so the row must too.
 */
export function isApproverException(flag: RiskFlag): boolean {
  return (
    flag.severity === "blocking" ||
    flag.flag_type === "policy_violation" ||
    flagJustification(flag) !== null
  );
}

/**
 * QA round: an entry_ref the agents use as an internal key rather than a
 * readable name — `entry_<n>` (an entry without a vendor, e.g. mileage),
 * `entry_<id>` / `segment_<id>`, or a bare UUID. Never shown as a title.
 */
export function isInternalEntryRef(ref: string | null | undefined): boolean {
  const r = (ref ?? "").trim();
  if (!r) return false;
  return (
    /^(entry|segment)_[0-9a-z-]+$/i.test(r) ||
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(r)
  );
}

/** Readable title for a flag's entry, or null to use the server label. */
export type FlagLabelResolver = (flag: RiskFlag) => string | null;

function expenseLabel(
  flag: RiskFlag,
  resolve: FlagLabelResolver | undefined,
): string {
  const ref = flag.entry_ref?.trim() || null;
  if (ref && !isInternalEntryRef(ref)) return ref;
  return resolve?.(flag) ?? (ref ? "Expense entry" : "Flagged item");
}

function expenseExceptions(
  flags: RiskFlag[],
  resolve?: FlagLabelResolver,
): ExceptionView[] {
  return flags.map((flag, index) => ({
    ref: `${flag.entry_ref ?? "flag"}-${index}`,
    label: expenseLabel(flag, resolve),
    reason: flag.reason ?? null,
    clauseRef: flag.source_clause_ref ?? null,
    severity: isApproverException(flag)
      ? "blocking"
      : normalizeSeverity(flag.severity),
    justification: flagJustification(flag),
    typeLabel: FLAG_TYPE_LABEL[flag.flag_type ?? ""] ?? null,
  }));
}

function tripInPolicyNote(flags: RiskFlag[]): string | null {
  const segments = flags.filter(isSegmentFlag);
  if (segments.length === 0) return null;
  const inPolicy = segments.filter((f) => f.in_policy === true).length;
  return `${inPolicy} of ${segments.length} segments in policy`;
}

export function toDecisionView(
  riskSummary: RiskSummary | null,
  subjectType: ApprovalSubjectType,
  resolveLabel?: FlagLabelResolver,
): DecisionView {
  if (!riskSummary) {
    return { aiAnalysis: EMPTY_ANALYSIS, exceptions: [], inPolicyNote: null };
  }

  const flags = riskSummary.flags ?? [];
  const isTrip = subjectType === "trip";
  // C7R: a failed re-evaluation is "not evaluated", whatever the other fields
  // say. Older snapshots without the marker: compliance null AND no total.
  const notEvaluated =
    riskSummary.evaluation_failed === true ||
    (riskSummary.reassessed_on_resubmission === true &&
      riskSummary.is_policy_compliant === null &&
      (riskSummary.subject_total_usd ?? null) === null);
  const reassessed = riskSummary.reassessed_on_resubmission === true;

  return {
    aiAnalysis: {
      route: riskSummary.route ?? null,
      inPolicy: notEvaluated ? null : (riskSummary.subject_in_policy ?? null),
      blockingCount: riskSummary.blocking_flag_count ?? 0,
      requiresApprover: riskSummary.requires_approver ?? false,
      totalUsd: riskSummary.subject_total_usd ?? null,
      notEvaluated,
      reassessed,
      reassessedAt:
        reassessed && typeof riskSummary.resubmitted_at === "string"
          ? riskSummary.resubmitted_at
          : null,
      rationale: stringList(riskSummary.policy_rationale),
    },
    exceptions: isTrip
      ? tripExceptions(flags)
      : expenseExceptions(flags, resolveLabel),
    inPolicyNote: isTrip ? tripInPolicyNote(flags) : null,
  };
}
