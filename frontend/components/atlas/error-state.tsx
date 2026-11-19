import * as React from "react";
import { AlertCircle } from "lucide-react";
import { AtlasButton } from "./button";
import { cn } from "@/lib/utils";

/**
 * Atlas Error State — error with retry.
 *
 * Spec: UI_UX.md → UI states → Error
 * Red alert circle 48px, heading 20px/500, subtext 14px Slate-500,
 * "Retry" (primary) + "Contact Support" (secondary).
 */

export interface ErrorStateProps {
  /** Error heading. */
  heading: string;
  /** Descriptive subtext. */
  subtext: string;
  /** Retry handler. */
  onRetry?: () => void;
  /** Secondary action handler. */
  onSecondary?: () => void;
  /** Secondary button label. Default: "Contact Support". */
  secondaryLabel?: string;
  className?: string;
}

export function ErrorState({
  heading,
  subtext,
  onRetry,
  onSecondary,
  secondaryLabel = "Contact Support",
  className,
}: ErrorStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center py-[var(--space-12)] px-[var(--space-4)]",
        "animate-fade-in-up",
        className,
      )}
      role="alert"
    >
      <div className="mb-[var(--space-4)] text-[var(--color-error)]">
        <AlertCircle className="h-12 w-12" aria-hidden="true" />
      </div>
      <h3 className="atlas-text-title text-[var(--color-primary-900)] text-center">
        {heading}
      </h3>
      <p className="atlas-text-body text-[#64748B] mt-2 text-center max-w-sm">
        {subtext}
      </p>
      <div className="flex items-center gap-3 mt-[var(--space-5)]">
        {onRetry && (
          <AtlasButton variant="primary" size="md" onClick={onRetry}>
            Retry
          </AtlasButton>
        )}
        {onSecondary && (
          <AtlasButton variant="secondary" size="md" onClick={onSecondary}>
            {secondaryLabel}
          </AtlasButton>
        )}
      </div>
    </div>
  );
}
