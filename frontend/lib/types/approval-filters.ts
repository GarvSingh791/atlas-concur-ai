/**
 * Approval filter-option DTOs (Product gap 6) — mirrors
 * backend/app/modules/approval/filter_options.py.
 */

/** One canonical exception type + how many approvals in YOUR scope carry it. */
export interface ExceptionTypeOption {
  value: string;
  label: string;
  /** "flag" (a check's flag type), "rule" (a policy rule type), "segment". */
  kind: "flag" | "rule" | "segment";
  count: number;
}
