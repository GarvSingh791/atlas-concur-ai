import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Atlas Confidence Indicator — maps to receipts.field_confidence / ai_decision_logs.confidence.
 *
 * Spec: UI_UX.md → Trust components → Confidence indicator
 * Bar: 120px × 4px, rounded-full. Fill: ≥90% green | 70–89% amber | <70% red.
 * Animation: progress-fill 500ms ease-out.
 */

export interface ConfidenceIndicatorProps {
  /** Confidence value 0–100. */
  value: number;
  /** Show "Confidence: N%" label. Defaults to true. */
  showLabel?: boolean;
  className?: string;
}

function getConfidenceColor(value: number): string {
  if (value >= 90) return "var(--color-success)";
  if (value >= 70) return "var(--color-warning)";
  return "var(--color-error)";
}

function getConfidenceLabel(value: number): string {
  if (value >= 90) return "High confidence";
  if (value >= 70) return "Medium confidence";
  return "Low confidence";
}

export function ConfidenceIndicator({
  value,
  showLabel = true,
  className,
}: ConfidenceIndicatorProps) {
  const clampedValue = Math.max(0, Math.min(100, value));
  const color = getConfidenceColor(clampedValue);
  const ariaLabel = getConfidenceLabel(clampedValue);

  return (
    <div
      className={cn(
        // Wraps (bar under the label) and shrinks when squeezed instead of
        // overflowing onto its neighbours; identical when there is room.
        "inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 gap-y-1",
        className,
      )}
      role="meter"
      aria-valuenow={clampedValue}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={ariaLabel}
    >
      {/* Size before leading: tailwind-merge lets a font-size class drop an
          EARLIER leading-* class. `length:` makes it a font-size (an untyped
          text-[var(--…)] compiles to a colour). */}
      {showLabel && (
        <span className="text-[length:var(--text-caption-size)] leading-[var(--text-caption-line-height)] text-[#64748B] whitespace-nowrap">
          Confidence: {clampedValue}%
        </span>
      )}
      <div
        className="relative h-1 w-[120px] min-w-[48px] shrink rounded-atlas-full overflow-hidden"
        style={{ backgroundColor: "#E2E8F0" }}
      >
        <div
          className="absolute inset-y-0 left-0 rounded-atlas-full animate-progress-fill"
          style={
            {
              backgroundColor: color,
              "--progress-width": `${clampedValue}%`,
              width: `${clampedValue}%`,
            } as React.CSSProperties
          }
        />
      </div>
    </div>
  );
}
