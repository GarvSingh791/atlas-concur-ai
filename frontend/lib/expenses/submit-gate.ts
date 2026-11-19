/**
 * Submit gate — P5-T7 (US-E4 AC3).
 *
 * Presentation-only gating computed from the readiness LEVEL (i.e. from server
 * flag SEVERITY). This is NOT a policy re-evaluation and NOT a total recompute —
 * it only decides how the submit affordance behaves:
 *
 *   green  (ready)  → submit enabled, no confirmation.
 *   amber  (review) → submit enabled, but open a warning-confirmation modal first.
 *   red    (issue)  → submit disabled, with a "resolve issues" tooltip.
 *
 * The server remains the source of truth — it independently rejects an illegal
 * transition; the gate is just the UX in front of it.
 */

import type { ReadinessLevel } from "./readiness";

export interface SubmitGate {
  color: "green" | "amber" | "red";
  /** Whether the submit button is interactive. */
  canSubmit: boolean;
  /** Whether a warning-confirmation modal must precede the submit. */
  requiresConfirm: boolean;
  /** Tooltip shown when submit is disabled (null when enabled). */
  tooltip: string | null;
}

export function computeSubmitGate(level: ReadinessLevel): SubmitGate {
  switch (level) {
    case "issue":
      return {
        color: "red",
        canSubmit: false,
        requiresConfirm: false,
        tooltip: "Resolve the flagged issues before submitting.",
      };
    case "review":
      return {
        color: "amber",
        canSubmit: true,
        requiresConfirm: true,
        tooltip: null,
      };
    default:
      return {
        color: "green",
        canSubmit: true,
        requiresConfirm: false,
        tooltip: null,
      };
  }
}
