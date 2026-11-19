/**
 * Pre-submit readiness normalizer — P5-T7 (US-E4 AC4, US-E5 AC2).
 *
 * `expense_reports.pre_submit_flags` is an UNTYPED JSONB passthrough on the
 * backend (schemas.py: `pre_submit_flags: dict | None`). Two real shapes reach
 * the client, so this is the single defensive normalization point:
 *
 *   A. Agent-emitted (ai_agents/.../presubmit.py build_flags_snapshot):
 *      { flags: [{ entry_ref, flag_type, source_clause_ref, reason,
 *                  severity: "blocking" | "advisory", ... }],
 *        blocking_count, advisory_count, evaluated_entry_count }
 *
 *   B. Seeded / legacy (database/seeds/seed_all.py — the rejected report):
 *      { warnings: [{ severity: "high" | "medium" | "low",
 *                     message, clause_ref }] }
 *
 * This module ONLY reshapes server data + maps severity → a display bucket. It
 * performs NO policy evaluation; the submit gate (submit-gate.ts) consumes the
 * resulting `level` — it never re-derives policy.
 */

/** Display severity bucket: red (must resolve) vs amber (informational). */
export type ReadinessSeverity = "issue" | "review";

export interface ReadinessFlag {
  severity: ReadinessSeverity;
  /** Plain-language explanation, straight from the server (reason | message). */
  message: string;
  /** Policy clause citation, if the server provided one. */
  clauseRef: string | null;
  /** The entry this flag references (vendor or "entry_{idx}"), if present. */
  entryRef: string | null;
  /** The flag_type (agent shape only) — drives the suggested-fix hint. */
  flagType: string | null;
  /** The persisted entry this flag is about (C7R `entry_id`), if present. */
  entryId?: string | null;
  /** The rule type behind the flag (e.g. "amount_cap"), if present. */
  ruleType?: string | null;
  /**
   * C7R UAT: the server says a written justification can resolve this flag
   * (a soft policy exception). Never derived client-side.
   */
  justifiable?: boolean;
  /** The justification the server applied to this flag (it is then advisory). */
  justification?: string | null;
  /**
   * Issue 6: the line is within the cap only at the claimant's UNVERIFIED stay
   * length / city ("nights", "days", "city", "nights_and_city",
   * "days_and_city"). Advisory for submit; the approver confirms it.
   */
  pendingVerification?: string | null;
  /**
   * EXP-04: the server says a missing-receipt affidavit may be signed for this
   * line (a `missing_receipt` flag whose receipt is required and missing, within
   * the affidavit amount). Never derived client-side.
   */
  affidavitAllowed?: boolean;
  /**
   * Known limits (ground): nothing trusted (receipt, card match) names what kind of
   * ride this ground-transport line is — a receipt or card match that names it
   * confirms the type. Server-provided (`observed.confirm_ground_type`).
   */
  confirmGroundType?: boolean;
}

/** Entry-time hint for a ground line whose kind nothing trusted confirms. */
export const CONFIRM_GROUND_TYPE_HINT =
  "Add a receipt or card match that names the kind of ride to confirm the type.";

/** Report-level readiness: green / amber / red. */
export type ReadinessLevel = "ready" | "review" | "issue";

export interface ReadinessSummary {
  level: ReadinessLevel;
  flags: ReadinessFlag[];
  issueCount: number;
  reviewCount: number;
}

const ISSUE_SEVERITIES = new Set(["blocking", "high", "critical"]);

/** Map a raw severity string to the display bucket. Unknown/missing → "review"
 *  (surface it, but never hard-block the user on an unrecognized value). */
function mapSeverity(raw: unknown): ReadinessSeverity {
  if (typeof raw === "string" && ISSUE_SEVERITIES.has(raw.toLowerCase())) {
    return "issue";
  }
  return "review";
}

function affidavitAllowed(observed: unknown): boolean | undefined {
  if (observed === null || typeof observed !== "object") return undefined;
  const value = (observed as Record<string, unknown>).affidavit_allowed;
  return typeof value === "boolean" ? value : undefined;
}

function confirmGroundType(observed: unknown): boolean | undefined {
  if (observed === null || typeof observed !== "object") return undefined;
  return (observed as Record<string, unknown>).confirm_ground_type === true
    ? true
    : undefined;
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v.length > 0 ? v : null;
}

/** Pull the flag array from either shape (`flags` or `warnings`). */
function extractRawFlags(raw: unknown): Record<string, unknown>[] {
  if (raw === null || typeof raw !== "object") return [];
  const obj = raw as Record<string, unknown>;
  const arr = Array.isArray(obj.flags)
    ? obj.flags
    : Array.isArray(obj.warnings)
      ? obj.warnings
      : [];
  return arr.filter(
    (f): f is Record<string, unknown> => f !== null && typeof f === "object",
  );
}

/**
 * Normalize a report's raw `pre_submit_flags` into a display-ready summary.
 * Handles null, `{flags:[...]}`, and `{warnings:[...]}` uniformly.
 */
export function normalizePreSubmitFlags(raw: unknown): ReadinessSummary {
  const rawFlags = extractRawFlags(raw);

  const flags: ReadinessFlag[] = rawFlags.map((f) => ({
    severity: mapSeverity(f.severity),
    message:
      asString(f.reason) ?? asString(f.message) ?? "Policy flag (no detail)",
    clauseRef: asString(f.source_clause_ref) ?? asString(f.clause_ref),
    entryRef: asString(f.entry_ref),
    flagType: asString(f.flag_type),
    entryId: asString(f.entry_id),
    ruleType: asString(f.rule_type),
    // undefined for snapshots written before the server emitted the key.
    justifiable: typeof f.justifiable === "boolean" ? f.justifiable : undefined,
    justification: asString(f.justification),
    pendingVerification: asString(f.pending_verification),
    affidavitAllowed: affidavitAllowed(f.observed),
    confirmGroundType: confirmGroundType(f.observed),
  }));

  const issueCount = flags.filter((f) => f.severity === "issue").length;
  const reviewCount = flags.filter((f) => f.severity === "review").length;

  const level: ReadinessLevel =
    issueCount > 0 ? "issue" : reviewCount > 0 ? "review" : "ready";

  return { level, flags, issueCount, reviewCount };
}

/** Issue 6: what the approver must confirm for a pending-verification flag. */
const PENDING_VERIFICATION_TEXT: Record<string, string> = {
  nights: "nights",
  days: "days",
  city: "city",
  nights_and_city: "nights and city",
  days_and_city: "days and city",
};

/** A short, presentation-only suggested fix derived from the agent flag_type.
 *  Falls back to a generic hint for the seed shape (which has no flag_type). */
export function suggestedFix(flag: ReadinessFlag): string {
  if (flag.justification) {
    return "Justification added — your approver will review this exception.";
  }
  const pending = flag.pendingVerification
    ? PENDING_VERIFICATION_TEXT[flag.pendingVerification]
    : undefined;
  if (pending) {
    return `Within policy for the ${pending} you stated — your approver will confirm before approval.`;
  }
  if (flag.confirmGroundType) {
    return CONFIRM_GROUND_TYPE_HINT;
  }
  switch (flag.flagType) {
    case "missing_receipt":
      // An attached-but-unverifiable receipt is advisory (UAT2): submittable,
      // a human compares it with the claim. Product gap 2: an accepted EXP-04
      // missing-receipt affidavit is advisory too.
      if (flag.severity !== "issue") {
        return flag.clauseRef === "EXP-04"
          ? "Affidavit signed — your approver reviews the explanation."
          : "Your approver will check this receipt against the claim.";
      }
      return flag.clauseRef === "EXP-04"
        ? "Attach the receipt — the missing-receipt affidavit can’t be used here (EXP-04)."
        : "Attach a receipt that covers this entry.";
    case "late_submission":
      // Product gap 2 (EXP-01): past the 30-day deadline — advisory.
      return "Submit this report soon — after 60 days it needs VP approval.";
    case "unreconciled_card":
      // Product gap 2 (CARD-03): older card transactions not in any report.
      return "Add your older card transactions to an expense report.";
    case "receipt_mismatch":
      return "Check the receipt matches this expense — attach the right one if not.";
    case "possible_duplicate":
      return "If this is a second claim for the same purchase, delete the extra entry.";
    case "approval_required":
      return "This will require approver sign-off after submission.";
    case "policy_advisory":
      // Product gap 2: in policy through the clause's own allowance (e.g. LOD-02).
      return "Within the policy's automatic allowance — no action needed.";
    case "not_evaluated":
      return "Couldn’t be auto-checked — review manually before submitting.";
    case "policy_violation":
      // Product gap 2: the fix for these is data on the entry, not prose.
      if (flag.ruleType === "gift_disclosure") {
        return "Edit the entry and complete the gift disclosure.";
      }
      if (flag.ruleType === "mileage") {
        return "Edit the entry and save the trip again — Atlas recomputes the amount.";
      }
      // Only offer the justification route when the server says it applies;
      // older snapshots (no `justifiable` key) keep the original wording.
      if (flag.justifiable === false) {
        return "This isn’t allowed by policy — change the category or amount.";
      }
      return "Adjust the amount to within policy, or add a justification.";
    default:
      return flag.severity === "issue"
        ? "Resolve this before submitting."
        : "Review before submitting.";
  }
}

const LEVEL_LABELS: Record<ReadinessLevel, string> = {
  ready: "Ready",
  review: "Needs review",
  issue: "Issue found",
};

export function readinessLabel(level: ReadinessLevel): string {
  return LEVEL_LABELS[level];
}
