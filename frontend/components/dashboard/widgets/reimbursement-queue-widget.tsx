"use client";

import * as React from "react";
import { BadgeCheck } from "lucide-react";

import {
  AtlasButton,
  EmptyState,
  ErrorState,
  LoadingState,
} from "@/components/atlas";
import { ReimburseReportDialog } from "@/components/expenses/lifecycle-dialogs";
import { useSession } from "@/lib/auth/session-provider";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import { useReimbursementQueue } from "@/lib/query/lifecycle-hooks";
import type { ReimbursementQueueItem } from "@/lib/types/lifecycle";

/**
 * "Ready to reimburse" — product gap 1.
 *
 * Finance's list of APPROVED expense reports with a "Mark reimbursed" action
 * (simulated payment, confirm dialog with amount / title / owner). Rendered only
 * for a session holding `expense.reimburse.org`; a finance user's OWN report is
 * listed without the action (segregation of duties — the server refuses it too).
 */
export function ReimbursementQueueWidget() {
  const { user, hasPermission } = useSession();
  const allowed = hasPermission("expense.reimburse.org");
  const queue = useReimbursementQueue(allowed);
  const [target, setTarget] = React.useState<ReimbursementQueueItem | null>(
    null,
  );

  if (!allowed) return null;

  let body: React.ReactNode;
  if (queue.isLoading) {
    body = <LoadingState variant="table" rows={3} />;
  } else if (queue.isError) {
    body = (
      <ErrorState
        heading="Couldn’t load reports to reimburse"
        subtext="Something went wrong fetching approved reports."
        onRetry={() => queue.refetch()}
      />
    );
  } else if (!queue.data || queue.data.items.length === 0) {
    body = (
      <EmptyState
        heading="Nothing to reimburse"
        subtext="Approved expense reports will appear here."
      />
    );
  } else {
    body = (
      <ul
        className="divide-y divide-border rounded-atlas-md border border-border bg-card"
        aria-label="Approved reports awaiting reimbursement"
      >
        {queue.data.items.map((item) => {
          const own = item.owner_id === user?.id;
          return (
            <li
              key={item.id}
              className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
              data-testid="reimbursement-row"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">
                  {item.title}
                </p>
                <p className="text-xs text-muted-foreground">
                  {item.owner_name ?? "Unknown employee"} · submitted{" "}
                  {formatDate(item.submitted_at)}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <span className="font-mono text-sm">
                  {formatCurrency(item.total_amount, item.currency)}
                </span>
                {item.can_reimburse && !own ? (
                  <AtlasButton
                    variant="secondary"
                    size="sm"
                    onClick={() => setTarget(item)}
                    aria-label={`Mark ${item.title} reimbursed`}
                  >
                    <BadgeCheck className="h-4 w-4" aria-hidden="true" />
                    Mark reimbursed
                  </AtlasButton>
                ) : (
                  <span className="atlas-text-caption text-muted-foreground">
                    {own
                      ? "Your report — another finance user pays it"
                      : "Not ready"}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <section className="flex flex-col gap-3" aria-label="Ready to reimburse">
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="text-lg font-semibold text-foreground">
          Ready to reimburse
        </h2>
        {queue.data && (
          <span className="atlas-text-caption text-muted-foreground">
            {queue.data.total} approved
          </span>
        )}
      </div>
      <p className="atlas-text-caption text-muted-foreground">
        Simulated payments: marking a report reimbursed records a payment
        reference — no real money moves.
      </p>
      {body}
      {target && (
        <ReimburseReportDialog
          report={target}
          onOpenChange={(open) => !open && setTarget(null)}
        />
      )}
    </section>
  );
}
