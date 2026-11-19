import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Atlas Loading State — skeleton shimmer blocks.
 *
 * Spec: UI_UX.md → UI states → Loading
 * Skeleton shimmer: linear gradient sweep L→R, 1.5s loop,
 * #E2E8F0 base → #F1F5F9 shimmer.
 */

export type LoadingVariant = "card" | "table" | "form";

export interface LoadingStateProps {
  /** Shape of the skeleton placeholder. */
  variant?: LoadingVariant;
  /** Number of rows/items to show. Default: 3. */
  rows?: number;
  className?: string;
}

function CardSkeleton() {
  return (
    <div className="rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)] p-[var(--space-6)] shadow-atlas-1">
      <div className="atlas-shimmer h-3 w-24 rounded mb-3" />
      <div className="atlas-shimmer h-8 w-20 rounded mb-2" />
      <div className="atlas-shimmer h-3 w-32 rounded" />
    </div>
  );
}

function TableRowSkeleton() {
  return (
    <div className="flex items-center gap-4 px-4 py-3 border-b border-[#F1F5F9]">
      <div className="atlas-shimmer h-4 w-4 rounded" />
      <div className="atlas-shimmer h-4 w-32 rounded" />
      <div className="atlas-shimmer h-4 w-20 rounded" />
      <div className="atlas-shimmer h-4 w-16 rounded" />
      <div className="atlas-shimmer h-5 w-20 rounded-full" />
      <div className="flex-1" />
      <div className="atlas-shimmer h-4 w-12 rounded" />
    </div>
  );
}

function FormFieldSkeleton() {
  return (
    <div className="space-y-2">
      <div className="atlas-shimmer h-3 w-20 rounded" />
      <div className="atlas-shimmer h-10 w-full rounded-atlas-md" />
    </div>
  );
}

export function LoadingState({
  variant = "card",
  rows = 3,
  className,
}: LoadingStateProps) {
  const count = Math.max(1, Math.min(rows, 10));

  if (variant === "card") {
    return (
      <div className={cn("grid gap-4 md:grid-cols-3", className)}>
        {Array.from({ length: count }, (_, i) => (
          <CardSkeleton key={i} />
        ))}
      </div>
    );
  }

  if (variant === "table") {
    return (
      <div
        className={cn(
          "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)] overflow-hidden",
          className,
        )}
      >
        {/* Header skeleton */}
        <div className="flex items-center gap-4 px-4 py-3 bg-[var(--surface-table-header)] border-b border-[#E2E8F0]">
          <div className="atlas-shimmer h-4 w-4 rounded" />
          <div className="atlas-shimmer h-3 w-24 rounded" />
          <div className="atlas-shimmer h-3 w-20 rounded" />
          <div className="atlas-shimmer h-3 w-16 rounded" />
          <div className="atlas-shimmer h-3 w-16 rounded" />
          <div className="flex-1" />
          <div className="atlas-shimmer h-3 w-12 rounded" />
        </div>
        {Array.from({ length: count }, (_, i) => (
          <TableRowSkeleton key={i} />
        ))}
      </div>
    );
  }

  // form variant
  return (
    <div className={cn("space-y-4 max-w-md", className)}>
      {Array.from({ length: count }, (_, i) => (
        <FormFieldSkeleton key={i} />
      ))}
    </div>
  );
}
