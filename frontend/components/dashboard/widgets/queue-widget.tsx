"use client";

import {
  LoadingState,
  EmptyState,
  ErrorState,
  StatusBadge,
} from "@/components/atlas";
import type { ApprovalQueueItem } from "@/lib/types/analytics";
import {
  formatMoney,
  statusBadgeVariant,
  statusLabel,
} from "@/lib/approvals/format";
import { ageText, subjectTypeLabel } from "@/lib/dashboard/format";

interface QueueWidgetProps {
  items: ApprovalQueueItem[] | undefined;
  total: number | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

export function QueueWidget({
  items,
  total,
  isLoading,
  isError,
  onRetry,
}: QueueWidgetProps) {
  if (isLoading) return <LoadingState variant="table" rows={5} />;

  if (isError) {
    return (
      <ErrorState
        heading="Failed to load approval queue"
        subtext="Could not fetch pending approvals."
        onRetry={onRetry}
      />
    );
  }

  if (!items || items.length === 0) {
    return (
      <EmptyState
        heading="No pending approvals"
        subtext="All approvals are up to date."
      />
    );
  }

  return (
    <div className="space-y-2">
      {total !== undefined && total > items.length && (
        <p className="text-xs text-muted-foreground">
          {items.length} of {total} shown
        </p>
      )}
      <div className="divide-y divide-border rounded-atlas-md border border-border bg-card">
        {items.map((item) => (
          <div
            key={item.id}
            className="flex items-center justify-between gap-3 px-4 py-3"
          >
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium truncate">
                {item.submitter_name}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {subjectTypeLabel(item.subject_type)} · {ageText(item.age_days)}
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0 sm:gap-3">
              {item.amount != null && (
                <span className="whitespace-nowrap text-sm font-mono">
                  {formatMoney(String(item.amount), item.currency ?? "USD")}
                </span>
              )}
              {/* Unknown statuses render a neutral badge with their own label,
                  never a misleading "Pending". */}
              <StatusBadge
                variant={statusBadgeVariant(item.status)}
                label={statusLabel(item.status)}
              />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
