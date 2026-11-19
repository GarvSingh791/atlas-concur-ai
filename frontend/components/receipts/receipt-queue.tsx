"use client";

import * as React from "react";
import {
  Loader2,
  CheckCircle2,
  AlertCircle,
  AlertTriangle,
  RotateCw,
  X,
  FileText,
} from "lucide-react";
import { AtlasButton } from "@/components/atlas";
import { cn } from "@/lib/utils";
import { receiptAmountNeedsInput } from "@/lib/receipts/fields";
import { displayFileName } from "@/lib/receipts/display-name";
import type { QueueItem } from "@/lib/receipts/use-upload-queue";

/**
 * Receipt queue — P5-T6.
 *
 * The per-file upload list. Each item shows its own state (uploading / ready /
 * failed). A processed item is selectable to load its review panel; a failed item
 * offers Retry (re-POST with the same idempotency key → no duplicate).
 */

export interface ReceiptQueueProps {
  items: QueueItem[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onRetry: (id: string) => void;
  onRemove: (id: string) => void;
  /**
   * UAT2: really delete a PROCESSED upload (the stored receipt and the entry it
   * auto-drafted). Omitted → X only drops the item from this list. Failed or
   * in-flight items always just leave the list.
   */
  onDelete?: (id: string) => void;
  className?: string;
}

/** Same predicate as `statusLabel`, so the icon and the label never disagree. */
function itemNeedsReview(item: QueueItem): boolean {
  return item.receipt?.needs_review === true;
}

// Decorative (aria-hidden): the status label text carries the meaning.
function StatusIcon({ item }: { item: QueueItem }) {
  if (item.status === "uploading") {
    return (
      <Loader2
        className="h-4 w-4 animate-spin text-[var(--color-primary-600)]"
        aria-hidden="true"
        data-status-icon="uploading"
      />
    );
  }
  if (item.status === "processed") {
    if (itemNeedsReview(item)) {
      return (
        <AlertTriangle
          className="h-4 w-4 text-[var(--color-warning)]"
          aria-hidden="true"
          data-status-icon="needs-review"
        />
      );
    }
    return (
      <CheckCircle2
        className="h-4 w-4 text-[var(--color-success)]"
        aria-hidden="true"
        data-status-icon="ready"
      />
    );
  }
  return (
    <AlertCircle
      className="h-4 w-4 text-[var(--color-error)]"
      aria-hidden="true"
      data-status-icon="failed"
    />
  );
}

function statusLabel(item: QueueItem): string {
  if (item.status === "uploading") return "Extracting…";
  if (item.status === "processed") {
    if (!item.receipt || !itemNeedsReview(item)) {
      return "Ready to review";
    }
    return receiptAmountNeedsInput(item.receipt)
      ? "Needs review — enter the total"
      : "Needs review";
  }
  return item.error ?? "Upload failed";
}

export function ReceiptQueue({
  items,
  selectedId,
  onSelect,
  onRetry,
  onRemove,
  onDelete,
  className,
}: ReceiptQueueProps) {
  if (items.length === 0) return null;

  return (
    <ul
      className={cn("flex flex-col gap-2", className)}
      aria-label="Upload queue"
    >
      {items.map((item) => {
        const selectable = item.status === "processed";
        // Display only: bidi / control characters stripped, extension kept.
        const name = displayFileName(item.fileName);
        const selected = item.id === selectedId;
        return (
          <li key={item.id}>
            <div
              className={cn(
                "flex items-center gap-3 rounded-atlas-md border px-[var(--space-3)] py-[var(--space-2)] transition-colors duration-atlas-fast",
                selected
                  ? "border-[var(--color-primary-600)] bg-[var(--color-primary-50)]"
                  : "border-border bg-card",
              )}
            >
              <FileText
                className="h-4 w-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />

              <button
                type="button"
                onClick={() => selectable && onSelect(item.id)}
                disabled={!selectable}
                aria-current={selected}
                className={cn(
                  "flex min-w-0 flex-1 flex-col items-start text-left",
                  selectable ? "cursor-pointer" : "cursor-default",
                )}
              >
                <span
                  className="atlas-text-body font-medium text-foreground truncate max-w-full"
                  title={name}
                  data-testid="queue-file-name"
                >
                  {name}
                </span>
                <span
                  className={cn(
                    "inline-flex items-center gap-1 atlas-text-caption",
                    item.status === "failed"
                      ? "text-[var(--color-error)]"
                      : "text-muted-foreground",
                  )}
                >
                  <StatusIcon item={item} />
                  {statusLabel(item)}
                </span>
              </button>

              <div className="flex shrink-0 items-center gap-1">
                {item.status === "failed" && item.retryable !== false && (
                  <AtlasButton
                    size="sm"
                    variant="ghost"
                    onClick={() => onRetry(item.id)}
                    aria-label={`Retry ${name}`}
                  >
                    <RotateCw className="h-4 w-4" aria-hidden="true" />
                  </AtlasButton>
                )}
                {onDelete && item.status === "processed" && item.receipt ? (
                  <AtlasButton
                    size="sm"
                    variant="ghost"
                    onClick={() => onDelete(item.id)}
                    aria-label={`Delete ${name}`}
                  >
                    <X className="h-4 w-4" aria-hidden="true" />
                  </AtlasButton>
                ) : (
                  <AtlasButton
                    size="sm"
                    variant="ghost"
                    onClick={() => onRemove(item.id)}
                    disabled={item.status === "uploading"}
                    aria-label={`Remove ${name}`}
                  >
                    <X className="h-4 w-4" aria-hidden="true" />
                  </AtlasButton>
                )}
              </div>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
