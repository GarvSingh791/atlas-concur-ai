/**
 * Policy DTO types — mirrors backend/app/modules/policy/schemas.py +
 * database/models/policy_rule.py byte-for-byte. Update both when the
 * schema changes.
 *
 * Trust vocabulary (UI_UX.md): policy_rules.status → active | draft | archived
 * drive the status dots; an authored draft carries the "✨ AI Generated" chip.
 */

export type PolicyRuleType =
  | "amount_cap"
  | "category_allow"
  | "cabin_class"
  | "per_diem"
  | "preferred_supplier"
  | "receipt_required";

export type PolicyAppliesTo =
  | "flight"
  | "hotel"
  | "rail"
  | "car"
  | "meals"
  | "all";

export type PolicyRuleStatus = "draft" | "active" | "archived";

export interface Policy {
  id: string;
  name: string;
  version: number;
  document_text: string | null;
  effective_from: string; // ISO date string
  is_active: boolean;
  /**
   * Follow-up: the CURRENT corpus policy (Helios T&E) in sections with clause
   * codes — what the Policy page shows. Null/absent → fall back to document_text.
   */
  document?: PolicyDocument | null;
}

export interface PolicyDocument {
  title: string;
  heading: string | null;
  sections: {
    title: string;
    clauses: { clause_ref: string; text: string }[];
  }[];
}

export interface PolicyRule {
  id: string;
  policy_id: string;
  /** Readable title built server-side from the rule (never free user text). */
  title: string;
  /** What the rule engine actually does with this rule (server describe_rule). */
  summary: string[];
  rule_type: PolicyRuleType;
  applies_to: PolicyAppliesTo;
  definition: Record<string, unknown>;
  source_clause_ref: string | null;
  status: PolicyRuleStatus;
  version: number; // sourced from the parent policy
  created_at: string; // ISO datetime string
  updated_at: string; // ISO datetime string
}

/** Request body for the NL rule-authoring endpoint (Policy Agent does the NL→rule). */
export interface AuthorRuleRequest {
  title: string;
  description?: string | null;
  rule_type?: string | null;
  applies_to?: string | null;
  parameters?: Record<string, unknown> | null;
}

/** Enriched authoring response — the SERVER's structured draft, rendered as-is. */
export interface AuthorRuleResponse {
  id: string;
  policy_id: string;
  title: string;
  status: PolicyRuleStatus; // always 'draft' from the authoring path
  rule_type: PolicyRuleType;
  applies_to: PolicyAppliesTo;
  definition: Record<string, unknown>;
  /** What the rule engine actually does with this draft (server describe_rule). */
  summary: string[];
  source_clause_ref: string | null;
  version: number;
  /**
   * The intent that produced THIS draft (server echo of the drafted audit's
   * intent). Optional: absent on older servers — the client then falls back to
   * the text submitted with the request (UAT2-G).
   */
  intent?: string | null;
}

/** PATCH body — draft is never a target (forward transitions only). */
export interface UpdateRuleStatusRequest {
  status: "active" | "archived";
}

/** One changed field of a rule — `definition.<key>` for a definition key (gap 8). */
export interface RuleFieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

/** One audited change of a rule (GET /policy/rules/{id}/history). */
export interface RuleHistoryEntry {
  id: string;
  action: string;
  label: string;
  at: string; // ISO datetime
  actor_name: string | null; // null = system (migration / seed)
  changes: RuleFieldChange[];
  note: string | null;
}

export interface RuleHistoryResponse {
  rule_id: string;
  title: string;
  created_at: string;
  entries: RuleHistoryEntry[];
}
