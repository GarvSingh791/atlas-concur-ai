import * as React from "react";
import { FileQuestion, type LucideIcon } from "lucide-react";
import { AtlasButton } from "./button";
import { cn } from "@/lib/utils";

/**
 * Atlas Empty State — reusable empty-data block.
 *
 * Spec: UI_UX.md → UI states → Empty
 * Illustration (icon ≤160px), heading 20px/500, subtext 14px Slate-500, CTA primary.
 */

export interface EmptyStateProps {
  /** Icon component from lucide-react. Default: FileQuestion. */
  icon?: LucideIcon;
  /** Empty state heading. */
  heading: string;
  /** Descriptive subtext. */
  subtext: string;
  /** CTA button label. */
  ctaLabel?: string;
  /** CTA click handler. */
  onCta?: () => void;
  className?: string;
}

export function EmptyState({
  icon: Icon = FileQuestion,
  heading,
  subtext,
  ctaLabel,
  onCta,
  className,
}: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center py-[var(--space-12)] px-[var(--space-4)]",
        "animate-fade-in-up",
        className,
      )}
    >
      <div className="mb-[var(--space-4)] text-[#CBD5E1]">
        <Icon className="h-20 w-20" strokeWidth={1} aria-hidden="true" />
      </div>
      <h3 className="atlas-text-title text-[var(--color-primary-900)] text-center">
        {heading}
      </h3>
      <p className="atlas-text-body text-[#64748B] mt-2 text-center max-w-sm">
        {subtext}
      </p>
      {ctaLabel && onCta && (
        <AtlasButton
          variant="primary"
          size="md"
          onClick={onCta}
          className="mt-[var(--space-5)]"
        >
          {ctaLabel}
        </AtlasButton>
      )}
    </div>
  );
}
