import * as React from "react";
import {
  Link2,
  FileText,
  CreditCard,
  MapPin,
  PenLine,
  Minus,
} from "lucide-react";

import { EntryReceipts } from "./entry-receipts";
import { cn } from "@/lib/utils";
import type { ExpenseEntry } from "@/lib/types";
import {
  CATEGORY_LABELS,
  formatCurrency,
  formatDate,
  matchPresentation,
} from "@/lib/expenses/format";

/**
 * Per-entry lineage — P5-T7.
 *
 * Renders ONLY what the entry DTO carries (match_source, trip_segment_id,
 * receipt_id and the entry's own metadata). When the entry has a linked receipt
 * (`receipt_id`), its image is shown via ReceiptImage, which fetches a scoped,
 * short-lived signed link from GET /receipts/{id} (C2, NFR-SEC4) — the server
 * re-checks scope, so an out-of-scope viewer just sees "Preview unavailable".
 * The hook lives in that child, so entries without a receipt render with no
 * query client. UAT2 F: EVERY receipt backing the line is shown (EntryReceipts),
 * not just the earliest — a correct receipt attached after a wrong one is visible. Nothing is fabricated; seeded entries with a null match_source
 * degrade to an honest "manually entered" note.
 *
 * C7R UAT: the provenance header is a static label, not a control — it used to
 * render a pencil icon and the duplicated "Unmatched · Unmatched", which read as
 * a broken edit button. Editing lives in the report detail's real "Edit" action.
 * The entry's justification (if any) is shown so approvers see it in context.
 */

function SourceIcon({
  source,
  hasReceipt,
}: {
  source: ExpenseEntry["match_source"];
  hasReceipt: boolean;
}) {
  switch (source) {
    case "receipt":
      return <FileText className="h-4 w-4" aria-hidden="true" />;
    case "card_txn":
      return <CreditCard className="h-4 w-4" aria-hidden="true" />;
    case "trip":
      return <MapPin className="h-4 w-4" aria-hidden="true" />;
    case "manual":
      return <PenLine className="h-4 w-4" aria-hidden="true" />;
    default:
      return hasReceipt ? (
        <FileText className="h-4 w-4" aria-hidden="true" />
      ) : (
        <Minus className="h-4 w-4" aria-hidden="true" />
      );
  }
}

/** One provenance phrase, never a repeated word ("Unmatched · Unmatched"). */
export function provenanceText(
  match: ReturnType<typeof matchPresentation>,
): string {
  if (match.isAuto) return match.label.replace(/^Auto · /, "Auto-matched · ");
  if (match.label === "Manual") return "Manual entry";
  if (match.label.startsWith("Manual · ")) {
    return match.label.replace(/^Manual · /, "Manual entry · ");
  }
  if (!match.isMatched) return "Unmatched — no automatic source";
  return match.label;
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="atlas-text-caption text-muted-foreground">{label}</dt>
      <dd className="atlas-text-body break-words text-foreground">{value}</dd>
    </div>
  );
}

export interface EntryLineageProps {
  entry: ExpenseEntry;
}

export function EntryLineage({ entry }: EntryLineageProps) {
  const match = matchPresentation(entry.match_source, entry.receipt_id);

  return (
    <div className="flex flex-col gap-3">
      {/* Provenance header */}
      <div
        className={cn(
          "inline-flex w-fit items-center gap-2 rounded-atlas-sm border px-2.5 py-1",
          match.isAuto
            ? "border-border bg-muted text-[var(--color-info)]"
            : "border-border bg-muted text-muted-foreground",
        )}
      >
        <SourceIcon
          source={entry.match_source}
          hasReceipt={Boolean(entry.receipt_id)}
        />
        <span className="atlas-text-caption font-medium">
          {provenanceText(match)}
        </span>
      </div>

      {/* The DTO fields that ground this entry. */}
      <dl className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Field label="Vendor" value={entry.vendor ?? "—"} />
        <Field label="Category" value={CATEGORY_LABELS[entry.category]} />
        <Field
          label="Amount"
          value={
            <span className="font-mono">
              {formatCurrency(entry.amount, entry.currency)}
            </span>
          }
        />
        <Field label="Date" value={formatDate(entry.expense_date)} />
        <Field label="Itemized" value={entry.is_itemized ? "Yes" : "No"} />
        {entry.description && (
          <Field label="Description" value={entry.description} />
        )}
        {entry.trip_segment_id && (
          <Field
            label="Trip segment"
            value={
              <span className="inline-flex items-center gap-1 font-mono atlas-text-caption">
                <Link2 className="h-3 w-3 shrink-0" aria-hidden="true" />
                {entry.trip_segment_id.slice(0, 8)}…
              </span>
            }
          />
        )}
      </dl>

      {entry.justification && (
        <div className="flex flex-col gap-0.5">
          <span className="atlas-text-caption text-muted-foreground">
            Justification
          </span>
          <p className="atlas-text-body whitespace-pre-wrap break-words text-foreground">
            {entry.justification}
          </p>
        </div>
      )}

      {/* Every receipt backing the line (UAT2 F), each via its own scoped,
          short-lived signed link (server-checked). */}
      <EntryReceipts entry={entry} />

      {/* Honest provenance notes — never a fabricated source. */}
      {entry.match_source === "receipt" && !entry.receipt_id && (
        <p className="atlas-text-caption text-muted-foreground">
          Linked from an uploaded receipt.
        </p>
      )}
      {!entry.match_source && !entry.receipt_id && (
        <p className="atlas-text-caption text-muted-foreground">
          Manually entered — no automatic source linked to this entry.
        </p>
      )}
    </div>
  );
}
