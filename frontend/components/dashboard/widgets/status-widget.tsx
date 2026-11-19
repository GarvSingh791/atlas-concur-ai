"use client";

import { EmptyState, LoadingState, ErrorState } from "@/components/atlas";
import type { Recommendation, PolicyWarning } from "@/lib/types/analytics";

// ---------------------------------------------------------------------------
// RecommendationsWidget — AI recommendations with purple styling
// ---------------------------------------------------------------------------

interface RecommendationsWidgetProps {
  recommendations: Recommendation[] | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

export function RecommendationsWidget({
  recommendations,
  isLoading,
  isError,
  onRetry,
}: RecommendationsWidgetProps) {
  if (isLoading) return <LoadingState variant="card" rows={2} />;
  if (isError)
    return (
      <ErrorState
        heading="Failed to load recommendations"
        subtext="Could not fetch AI recommendations."
        onRetry={onRetry}
      />
    );
  if (!recommendations || recommendations.length === 0) {
    return (
      <EmptyState
        heading="No recommendations"
        subtext="AI recommendations will appear here."
      />
    );
  }
  return (
    <div className="space-y-3">
      {recommendations.map((rec) => (
        <div
          key={rec.id}
          className="rounded-atlas-md border border-[var(--color-ai-border,#E9D5FF)] bg-[var(--color-ai-surface,#FAF5FF)] p-4"
        >
          <p className="text-sm font-medium text-[var(--color-ai-text,#6D28D9)]">
            {rec.title}
          </p>
          <p className="text-xs text-muted-foreground mt-1">{rec.body}</p>
          {rec.citation && (
            <p className="text-xs text-muted-foreground mt-1 italic">
              {rec.citation}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// PolicyWarningsWidget — policy warnings with amber/red left-border treatment
// ---------------------------------------------------------------------------

interface PolicyWarningsWidgetProps {
  warnings: PolicyWarning[] | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

function warningBorderColor(severity: string): string {
  switch (severity) {
    case "high":
      return "var(--color-error, #DC2626)";
    case "medium":
      return "var(--color-warning, #D97706)";
    default:
      return "var(--color-warning, #D97706)";
  }
}

export function PolicyWarningsWidget({
  warnings,
  isLoading,
  isError,
  onRetry,
}: PolicyWarningsWidgetProps) {
  if (isLoading) return <LoadingState variant="card" rows={2} />;
  if (isError)
    return (
      <ErrorState
        heading="Failed to load policy warnings"
        subtext="Could not fetch policy warnings."
        onRetry={onRetry}
      />
    );
  if (!warnings || warnings.length === 0) {
    return (
      <EmptyState
        heading="No policy warnings"
        subtext="You're in compliance. Policy warnings will appear here if any are found."
      />
    );
  }
  return (
    <div className="space-y-3">
      {warnings.map((warning) => (
        <div
          key={warning.id}
          className="rounded-atlas-md border-l-4 bg-card p-4"
          style={{ borderLeftColor: warningBorderColor(warning.severity) }}
        >
          <p className="text-sm font-medium text-foreground">
            {warning.message}
          </p>
          {warning.source_clause_ref && (
            <p className="text-xs text-muted-foreground mt-1">
              {warning.source_clause_ref}
            </p>
          )}
        </div>
      ))}
    </div>
  );
}
