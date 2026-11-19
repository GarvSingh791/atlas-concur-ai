"use client";

import * as React from "react";
import { Check, PenLine, Minus, Pencil } from "lucide-react";

import {
  AtlasButton,
  DataTable,
  StatusBadge,
  type ColumnDef,
} from "@/components/atlas";
import { cn } from "@/lib/utils";
import { RowActions } from "@/components/atlas/row-actions";
import { EntryLineage } from "./entry-lineage";
import { EntryPolicyDetails } from "./entry-policy-details";
import { entryReceiptCount } from "./entry-receipts";
import type { ExpenseEntry } from "@/lib/types";
import type { EntryPolicyVerdict } from "@/lib/approvals/entry-policy";
import {
  CATEGORY_ICONS,
  CATEGORY_LABELS,
  formatCurrency,
  matchPresentation,
} from "@/lib/expenses/format";
import { AmountConversionNote } from "@/components/atlas/amount-conversion-note";

/**
 * Report entries table — P5-T7 (US-E4 AC1/AC2).
 *
 * Every cell maps 1:1 from the entry DTO — no client-side matching,
 * categorization, or policy evaluation. The MATCH column distinguishes
 * auto-matched (receipt/card/trip) from manual via `match_source`; the POLICY
 * column reflects the entry's own server `status` (flagged → out of policy).
 * Each row expands to its lineage.
 *
 * C7R: the MATCH column also honours a linked `receipt_id` (a manual entry with
 * an attached receipt is not a bare "Manual" line), and callers that hold a
 * fresher policy verdict (the approval detail's risk_summary flags) pass it via
 * `policyVerdicts`; a blocking flag always wins over the entry status.
 */

function MatchCell({ entry }: { entry: ExpenseEntry }) {
  const m = matchPresentation(entry.match_source, entry.receipt_id);
  const Icon = m.isAuto ? Check : m.isMatched ? PenLine : Minus;
  // UAT2 F: say up front when a line carries more than one receipt.
  const count = entryReceiptCount(entry);
  const chip = (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-atlas-sm border border-border px-2 py-0.5",
        "atlas-text-caption font-medium",
        m.isAuto
          ? "bg-muted text-[var(--color-info)]"
          : "bg-muted text-muted-foreground",
      )}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {m.label}
    </span>
  );
  if (count < 2) return chip;
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      {chip}
      <span className="atlas-text-caption text-muted-foreground">
        {count} receipts
      </span>
    </span>
  );
}

/** Readable row name for control labels ("Edit Grand Hotel"). */
export function entryLabel(e: ExpenseEntry): string {
  return e.vendor ?? e.description ?? `${CATEGORY_LABELS[e.category]} entry`;
}

export interface EntriesTableProps {
  entries: ExpenseEntry[];
  state?: "default" | "loading" | "empty" | "error";
  onRetry?: () => void;
  /** Per-entry verdict derived from server flags (entry id → verdict). */
  policyVerdicts?: Map<string, EntryPolicyVerdict>;
  /**
   * C7R UAT: when set (draft report, owner viewing), each row gets a real
   * "Edit" control. Omitted → the table is read-only.
   */
  onEditEntry?: (entry: ExpenseEntry) => void;
  /** C7R UAT: row selection (for moving entries to another report). */
  selection?: {
    selected: Set<string>;
    onChange: (keys: Set<string>) => void;
  };
  /** Overrides the empty-state subtext (e.g. how to add entries). */
  emptySubtext?: string;
  /**
   * UAT2: extra per-row controls (e.g. Attach receipt / Delete) rendered next
   * to Edit. Clicks never expand the row.
   */
  rowActions?: (entry: ExpenseEntry) => React.ReactNode;
}

function PolicyCell({
  entry,
  verdict,
}: {
  entry: ExpenseEntry;
  verdict: EntryPolicyVerdict | undefined;
}) {
  if (verdict === "blocking" || entry.status === "flagged") {
    return <StatusBadge variant="out-of-policy" />;
  }
  if (verdict === "ambiguous") {
    // A blocking flag names this line's vendor, but more than one line shares
    // it — do not claim either "Out of policy" or "In policy".
    return <StatusBadge variant="policy-advisory" label="Check exceptions" />;
  }
  if (verdict === "advisory") {
    return <StatusBadge variant="policy-advisory" />;
  }
  return <StatusBadge variant="in-policy" />;
}

export function EntriesTable({
  entries,
  state = "default",
  onRetry,
  policyVerdicts,
  onEditEntry,
  selection,
  emptySubtext,
  rowActions,
}: EntriesTableProps) {
  const columns: ColumnDef<ExpenseEntry>[] = [
    {
      key: "category",
      header: "Category",
      render: (e) => {
        const Icon = CATEGORY_ICONS[e.category];
        return (
          <span className="inline-flex items-center gap-2 text-foreground">
            <Icon
              className="h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            {CATEGORY_LABELS[e.category]}
          </span>
        );
      },
    },
    {
      key: "vendor",
      header: "Description",
      render: (e) => (
        <div className="flex flex-col gap-0.5">
          <span className="text-foreground">
            {e.vendor ?? e.description ?? "—"}
          </span>
          {e.vendor && e.description && (
            <span className="atlas-text-caption text-muted-foreground">
              {e.description}
            </span>
          )}
          <EntryPolicyDetails entry={e} />
        </div>
      ),
    },
    {
      key: "amount",
      header: "Amount",
      render: (e) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-mono text-foreground">
            {formatCurrency(e.amount, e.currency)}
          </span>
          <AmountConversionNote
            conversion={e.amount_conversion}
            compact
            data-testid={`entry-amount-conversion-${e.id}`}
          />
        </div>
      ),
    },
    {
      key: "match",
      header: "Match",
      render: (e) => <MatchCell entry={e} />,
    },
    {
      key: "policy",
      header: "Policy",
      render: (e) => (
        <PolicyCell entry={e} verdict={policyVerdicts?.get(e.id)} />
      ),
    },
  ];

  if (onEditEntry || rowActions) {
    columns.push({
      key: "actions",
      header: "",
      width: rowActions ? "w-[200px]" : "w-[84px]",
      render: (e) => (
        // The row is expandable: keep control clicks from toggling it.
        <span
          className="inline-flex justify-end"
          onClick={(ev) => ev.stopPropagation()}
          onKeyDown={(ev) => ev.stopPropagation()}
        >
          {/* Follow-up: one line (nowrap); folded behind a toggle below sm. */}
          <RowActions label={`Actions for ${entryLabel(e)}`}>
            {onEditEntry && (
              <AtlasButton
                variant="ghost"
                size="sm"
                onClick={() => onEditEntry(e)}
                aria-label={`Edit ${entryLabel(e)}`}
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />
                Edit
              </AtlasButton>
            )}
            {rowActions?.(e)}
          </RowActions>
        </span>
      ),
    });
  }

  return (
    <DataTable<ExpenseEntry>
      columns={columns}
      data={entries}
      getRowKey={(e) => e.id}
      state={state}
      expandable
      renderExpanded={(e) => <EntryLineage entry={e} />}
      selectable={Boolean(selection)}
      selectedKeys={selection?.selected}
      onSelectionChange={selection?.onChange}
      emptyState={{
        heading: "No entries",
        subtext: emptySubtext ?? "This report has no expense entries yet.",
      }}
      errorState={{
        heading: "Couldn’t load entries",
        subtext: "Something went wrong fetching this report’s entries.",
        onRetry,
      }}
    />
  );
}
