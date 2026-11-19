import * as React from "react";
import { AlertTriangle, HelpCircle, Link2, RotateCcw } from "lucide-react";

import { ReceiptImage } from "@/components/receipts/receipt-image";
import { cn } from "@/lib/utils";
import type { EntryReceipt, ExpenseEntry } from "@/lib/types";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import { displayText } from "@/lib/receipts/display-name";

/**
 * Every receipt backing an entry — UAT2 defect F.
 *
 * The approval detail used to show only the EARLIEST receipt per line, so a
 * wrong receipt attached first hid the correct one attached after a return.
 * This lists them all (direct links and folios shared from itemized siblings),
 * in the server's order, each with its own scoped, short-lived image link
 * (ReceiptImage → GET /receipts/{id}, which re-checks RBAC + record scope).
 *
 * Vendor / date / amount are the receipt's MACHINE reading as the server sends
 * it (`entry.receipts`); nothing is re-derived here. The markers are the
 * server's own verdicts: `covers_line` (RECEIPT-REQ's rule) and
 * `attached_after_return`. An older backend without `receipts` degrades to the
 * bare ids with no markers.
 */

/** The entry's receipts: the server list, else the bare ids (older backend). */
export function entryReceipts(entry: ExpenseEntry): {
  receipts: EntryReceipt[];
  detailed: boolean;
} {
  if (entry.receipts) return { receipts: entry.receipts, detailed: true };
  const ids = entry.receipt_ids ?? (entry.receipt_id ? [entry.receipt_id] : []);
  return {
    detailed: false,
    receipts: ids.map((receipt_id) => ({
      receipt_id,
      link: "direct" as const,
      vendor: null,
      receipt_date: null,
      amount: null,
      currency: null,
      covers_line: null,
      attached_after_return: false,
    })),
  };
}

/** How many receipts back the entry (server list, else ids). */
export function entryReceiptCount(entry: ExpenseEntry): number {
  return entryReceipts(entry).receipts.length;
}

function Marker({
  tone,
  icon: Icon,
  children,
}: {
  tone: "warning" | "info" | "muted";
  icon: React.ElementType;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-atlas-sm border px-2 py-0.5 atlas-text-caption font-medium",
        tone === "warning" &&
          "border-[#FDE68A] bg-[#FFFBEB] text-[var(--color-warning)]",
        tone === "info" && "border-border bg-muted text-[var(--color-info)]",
        tone === "muted" && "border-border bg-muted text-muted-foreground",
      )}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {children}
    </span>
  );
}

function ReceiptCard({
  entry,
  receipt,
  index,
  total,
  detailed,
}: {
  entry: ExpenseEntry;
  receipt: EntryReceipt;
  index: number;
  total: number;
  detailed: boolean;
}) {
  // Display only: OCR'd vendor text may carry bidi / control characters.
  const vendor = displayText(receipt.vendor, 80);
  const name = vendor || displayText(entry.vendor, 80) || "Receipt";
  const facts = [
    receipt.receipt_date ? formatDate(receipt.receipt_date) : null,
    receipt.amount
      ? formatCurrency(receipt.amount, receipt.currency ?? entry.currency)
      : null,
  ];
  return (
    <div
      role="group"
      aria-label={`Receipt ${index + 1} of ${total}: ${name}`}
      className="flex min-w-0 flex-col gap-2"
    >
      {detailed && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="atlas-text-body font-medium text-foreground">
              {vendor || "Vendor not read"}
            </span>
            {facts.map(
              (fact, i) =>
                fact && (
                  <span
                    key={i}
                    className={cn(
                      "atlas-text-caption text-muted-foreground",
                      i === 1 && "font-mono",
                    )}
                  >
                    {fact}
                  </span>
                ),
            )}
          </div>
          <div className="flex flex-wrap gap-1">
            {receipt.covers_line === false && (
              <Marker tone="warning" icon={AlertTriangle}>
                Doesn’t cover this line
              </Marker>
            )}
            {receipt.covers_line === null && (
              <Marker tone="muted" icon={HelpCircle}>
                Total not machine-read
              </Marker>
            )}
            {receipt.attached_after_return && (
              <Marker tone="info" icon={RotateCcw}>
                Added after the last return
              </Marker>
            )}
            {receipt.link === "shared" && (
              <Marker tone="muted" icon={Link2}>
                Shared with another line
              </Marker>
            )}
          </div>
        </div>
      )}
      <ReceiptImage receiptId={receipt.receipt_id} fileName={name} />
    </div>
  );
}

export function EntryReceipts({ entry }: { entry: ExpenseEntry }) {
  const { receipts, detailed } = entryReceipts(entry);
  if (receipts.length === 0) return null;
  const many = receipts.length > 1;
  return (
    <div className="flex flex-col gap-2">
      <span className="atlas-text-caption text-muted-foreground">
        {many ? `Receipts (${receipts.length})` : "Receipt"}
      </span>
      <div
        className={cn(
          "grid gap-3",
          many ? "grid-cols-1 md:grid-cols-2" : "max-w-md",
        )}
      >
        {receipts.map((r, i) => (
          <ReceiptCard
            key={r.receipt_id}
            entry={entry}
            receipt={r}
            index={i}
            total={receipts.length}
            detailed={detailed}
          />
        ))}
      </div>
    </div>
  );
}
