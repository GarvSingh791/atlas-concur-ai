import * as React from "react";

import { StatusBadge } from "@/components/atlas";
import { formatMoney } from "@/lib/approvals/format";
import type { ApprovalTripSegment } from "@/lib/types/approvals";

/**
 * Trip subject context — the trip's segments rendered for the decision panel
 * (the trip analogue of the expense EntriesTable). Each row maps 1:1 from the
 * segment DTO; the policy badge reflects the server `in_policy` value.
 */
export function TripSegments({
  segments,
}: {
  segments: ApprovalTripSegment[];
}) {
  if (segments.length === 0) {
    return (
      <p className="atlas-text-body text-muted-foreground">
        This trip has no segments.
      </p>
    );
  }

  return (
    <ul className="flex flex-col divide-y divide-border rounded-atlas-md border border-border">
      {segments.map((s) => (
        <li
          key={s.id}
          className="flex items-start justify-between gap-3 p-[var(--space-3)]"
        >
          <div className="flex min-w-0 flex-col">
            <span className="atlas-text-body capitalize text-foreground">
              {s.segment_type}
              {s.supplier ? ` · ${s.supplier}` : ""}
            </span>
            {s.location && (
              <span className="atlas-text-caption text-muted-foreground">
                {s.location}
              </span>
            )}
            {s.policy_notes && (
              <span className="atlas-text-caption text-[var(--color-warning)] mt-0.5">
                {s.policy_notes}
              </span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="font-mono atlas-text-caption text-foreground">
              {formatMoney(s.amount, s.currency)}
            </span>
            <StatusBadge
              variant={s.in_policy === false ? "out-of-policy" : "in-policy"}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
