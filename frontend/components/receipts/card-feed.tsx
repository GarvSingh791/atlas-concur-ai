"use client";

import * as React from "react";
import Link from "next/link";
import { CreditCard, Paperclip, Upload } from "lucide-react";

import {
  AtlasButton,
  EmptyState,
  ErrorState,
  LoadingState,
  StatusBadge,
} from "@/components/atlas";
import { AmountConversionNote } from "@/components/atlas/amount-conversion-note";
import { Input } from "@/components/ui/input";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import { useCardTransactions } from "@/lib/query/card-hooks";
import { validationErrorMessage } from "@/lib/types/api";
import type { CardTransaction, CardTransactionStatus } from "@/lib/types/cards";

/**
 * Card feed — product gap 8.
 *
 * The caller's OWN corporate-card swipes (GET /card-transactions; the server
 * scopes to the cardholder for every role). Each row shows merchant, date,
 * amount (with the base-currency conversion when foreign) and a matched /
 * unmatched badge; an unmatched swipe offers "Upload receipt", a matched one
 * links to the expense it backs. Read-only: matching is done by the server.
 */

const PAGE_SIZE = 20;

type StatusFilter = "all" | CardTransactionStatus;

export function expenseHref(reportId: string): string {
  return `/expenses?report=${encodeURIComponent(reportId)}`;
}

function CardRow({
  txn,
  onUploadReceipt,
}: {
  txn: CardTransaction;
  onUploadReceipt: () => void;
}) {
  const matched = txn.status === "matched";
  return (
    <li
      data-testid={`card-txn-${txn.id}`}
      className="flex flex-col gap-2 border-b border-border py-[var(--space-3)] last:border-b-0 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="atlas-text-body font-medium text-foreground truncate">
          {txn.merchant ?? "Unknown merchant"}
        </span>
        <span className="atlas-text-caption text-muted-foreground">
          {formatDate(txn.txn_date)}
          {txn.last_four ? ` · Card ending ${txn.last_four}` : ""}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-3 sm:justify-end">
        <div className="flex flex-col items-start sm:items-end">
          <span className="font-mono text-foreground">
            {formatCurrency(txn.amount, txn.currency)}
          </span>
          <AmountConversionNote
            conversion={txn.amount_conversion}
            compact
            data-testid={`card-txn-conversion-${txn.id}`}
          />
        </div>
        <StatusBadge
          variant={matched ? "approved" : "pending"}
          label={matched ? "Matched" : "Unmatched"}
        />
        {matched ? (
          txn.report_id ? (
            <Link
              href={expenseHref(txn.report_id)}
              className="inline-flex items-center gap-1 atlas-text-caption font-medium text-[var(--color-primary-600)] hover:underline"
              data-testid={`card-txn-link-${txn.id}`}
            >
              {txn.receipt_id && (
                <Paperclip className="h-3 w-3" aria-hidden="true" />
              )}
              {txn.receipt_id ? "View expense & receipt" : "View expense"}
            </Link>
          ) : null
        ) : (
          <AtlasButton
            size="sm"
            variant="secondary"
            onClick={onUploadReceipt}
            data-testid={`card-txn-upload-${txn.id}`}
          >
            <Upload className="h-3.5 w-3.5" aria-hidden="true" />
            Upload receipt
          </AtlasButton>
        )}
      </div>
    </li>
  );
}

export function CardFeed({
  onUploadReceipt,
}: {
  /** Switch to the upload view (the receipt is matched to the swipe server-side). */
  onUploadReceipt: () => void;
}) {
  const [status, setStatus] = React.useState<StatusFilter>("all");
  const [dateFrom, setDateFrom] = React.useState("");
  const [dateTo, setDateTo] = React.useState("");
  const [page, setPage] = React.useState(1);
  const invalidRange = dateFrom !== "" && dateTo !== "" && dateFrom > dateTo;

  const query = useCardTransactions(
    {
      status: status === "all" ? undefined : status,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      page,
      size: PAGE_SIZE,
    },
    // An inverted range is a 400 on the server — don't send it.
    { enabled: !invalidRange },
  );

  const reset = (fn: () => void) => {
    fn();
    setPage(1);
  };

  const data = query.data;
  const items = data?.items ?? [];

  return (
    <section
      className="flex flex-col gap-[var(--space-4)] rounded-atlas-md border border-border bg-card p-[var(--space-4)]"
      aria-label="Card feed"
      data-testid="card-feed"
    >
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 atlas-text-caption text-muted-foreground">
          Status
          <select
            value={status}
            onChange={(e) =>
              reset(() => setStatus(e.target.value as StatusFilter))
            }
            className="h-9 rounded-atlas-md border border-border bg-background px-2 atlas-text-body text-foreground"
            data-testid="card-feed-status"
          >
            <option value="all">All</option>
            <option value="unmatched">Unmatched</option>
            <option value="matched">Matched</option>
          </select>
        </label>
        <label className="flex flex-col gap-1 atlas-text-caption text-muted-foreground">
          From
          <Input
            type="date"
            value={dateFrom}
            onChange={(e) => reset(() => setDateFrom(e.target.value))}
            className="h-9"
            data-testid="card-feed-from"
          />
        </label>
        <label className="flex flex-col gap-1 atlas-text-caption text-muted-foreground">
          To
          <Input
            type="date"
            value={dateTo}
            onChange={(e) => reset(() => setDateTo(e.target.value))}
            className="h-9"
            data-testid="card-feed-to"
          />
        </label>
      </div>

      {invalidRange ? (
        <p
          role="alert"
          className="atlas-text-caption text-[var(--color-error)]"
          data-testid="card-feed-range-error"
        >
          The start date must be on or before the end date.
        </p>
      ) : query.isLoading ? (
        <LoadingState variant="table" rows={4} />
      ) : query.isError ? (
        <ErrorState
          heading="Couldn’t load your card feed"
          // QA round: a rejected filter says what the server objected to.
          subtext={validationErrorMessage(query.error) ?? "Please try again."}
          onRetry={() => void query.refetch()}
        />
      ) : items.length === 0 ? (
        <EmptyState
          icon={CreditCard}
          heading="No card transactions"
          subtext={
            status === "all" && !dateFrom && !dateTo
              ? "Swipes on your corporate card will appear here."
              : "No card transactions match these filters."
          }
        />
      ) : (
        <>
          <ul className="flex flex-col" aria-label="Card transactions">
            {items.map((txn) => (
              <CardRow
                key={txn.id}
                txn={txn}
                onUploadReceipt={onUploadReceipt}
              />
            ))}
          </ul>
          {data && data.pages > 1 && (
            <div className="flex items-center justify-between atlas-text-caption text-muted-foreground">
              <span>
                Page {data.page} of {data.pages} · {data.total} transactions
              </span>
              <div className="flex gap-2">
                <AtlasButton
                  size="sm"
                  variant="ghost"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  Previous
                </AtlasButton>
                <AtlasButton
                  size="sm"
                  variant="ghost"
                  disabled={page >= data.pages}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next
                </AtlasButton>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
