"use client";

import * as React from "react";
import { Pencil, Check, X, AlertTriangle } from "lucide-react";
import {
  ProvenanceBadge,
  ConfidenceIndicator,
  AtlasButton,
} from "@/components/atlas";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { FieldRow as FieldRowModel } from "@/lib/receipts/fields";
import type { GroundingContext, ReceiptFieldKey } from "@/lib/types";
import { GapFillExplainer } from "./gap-fill-explainer";

/**
 * Field-row — P5-T6.
 *
 * One row of the extraction-review table: Value | Confidence | Source(provenance),
 * with a below-threshold flag, the gap-fill explainer for AI-inferred fields, and
 * inline correction. The Source badge always reflects the SERVER's provenance —
 * the row re-renders from the PATCH response, it never sets provenance itself.
 */

/**
 * Grid for one field row (and the column header). The template lives in
 * styles/globals.css (`.atlas-field-grid` + `@container atlas-fields`): it
 * follows the TABLE's width, not the viewport — a two-line card when the table
 * is narrow (receipt image beside it, 1024–~1700px screens), the four-column
 * table when it is wide. Do NOT add grid-cols-* / md:grid utilities here or on
 * the header: they would override the container-query template. Each child
 * cell carries a `data-cell` grid-area hook.
 */
export const FIELD_ROW_GRID = "atlas-field-grid";

export interface FieldRowProps {
  row: FieldRowModel;
  grounding?: GroundingContext | null;
  onSave: (key: ReceiptFieldKey, value: string) => Promise<void> | void;
  saving?: boolean;
  /** UAT2 hardening: relied-on evidence — no Edit / Enter controls. */
  locked?: boolean;
}

export function FieldRow({
  row,
  grounding,
  onSave,
  saving,
  locked = false,
}: FieldRowProps) {
  const [editing, setEditing] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const inputId = React.useId();

  const startEdit = () => {
    setDraft(row.rawValue == null ? "" : String(row.rawValue));
    setEditing(true);
  };

  const cancelEdit = () => setEditing(false);

  const commit = async () => {
    try {
      await onSave(row.key, draft);
      setEditing(false);
    } catch {
      // onSave rejected the input (already reported) — keep the editor open
      // so the user can fix it.
    }
  };

  const isEdited = row.provenance === "user_edited";

  return (
    <div
      className={cn(
        "border-b border-border py-[var(--space-3)] last:border-b-0",
        isEdited &&
          "border-l-2 border-l-[var(--color-success)] pl-[var(--space-3)]",
        row.requiresUserInput &&
          "border-l-2 border-l-[var(--color-warning)] pl-[var(--space-3)]",
      )}
      data-requires-input={row.requiresUserInput || undefined}
    >
      <div className={FIELD_ROW_GRID}>
        {/* Field label */}
        <div
          data-cell="label"
          className="min-w-0 break-words atlas-text-body font-medium text-foreground"
        >
          {row.label}
        </div>

        {/* Value (display / inline edit) */}
        <div data-cell="value" className="min-w-0">
          {editing && !locked ? (
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id={inputId}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                aria-label={`${row.label} value`}
                placeholder={
                  row.requiresUserInput ? "Total from the receipt" : undefined
                }
                inputMode={row.key === "amount" ? "decimal" : undefined}
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === "Enter") void commit();
                  if (e.key === "Escape") cancelEdit();
                }}
                className="h-9 min-w-[6rem] flex-1"
              />
              <AtlasButton
                size="sm"
                variant="primary"
                loading={saving}
                onClick={() => void commit()}
                aria-label={`Save ${row.label}`}
              >
                <Check className="h-4 w-4" aria-hidden="true" />
              </AtlasButton>
              <AtlasButton
                size="sm"
                variant="ghost"
                onClick={cancelEdit}
                disabled={saving}
                aria-label={`Cancel editing ${row.label}`}
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </AtlasButton>
            </div>
          ) : row.requiresUserInput && !locked ? (
            // C7R: needs-review receipt — nothing is prefilled; the user types
            // the total themselves.
            <div className="flex flex-wrap items-center gap-2">
              <span className="atlas-text-caption text-[var(--color-warning)]">
                Not read reliably — enter it from the receipt
              </span>
              <AtlasButton
                size="sm"
                variant="secondary"
                onClick={startEdit}
                aria-label={`Enter ${row.label}`}
              >
                <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                Enter {row.label.toLowerCase()}
              </AtlasButton>
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <span
                className={cn(
                  "atlas-text-body min-w-0 truncate",
                  row.hasValue ? "text-foreground" : "text-muted-foreground",
                )}
                title={row.hasValue ? row.value : undefined}
              >
                {row.hasValue ? row.value : "—"}
              </span>
              {!locked && (
                <button
                  type="button"
                  onClick={startEdit}
                  aria-label={`Edit ${row.label}`}
                  className="shrink-0 rounded-atlas-sm p-1 text-muted-foreground transition-colors duration-atlas-fast hover:text-[var(--color-primary-600)]"
                >
                  <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                </button>
              )}
            </div>
          )}
        </div>

        {/* Confidence + below-threshold flag */}
        <div data-cell="confidence" className="flex min-w-0 flex-col gap-1">
          {row.confidence !== null ? (
            <ConfidenceIndicator value={row.confidence} />
          ) : (
            <span className="atlas-text-caption text-muted-foreground">—</span>
          )}
          {row.belowThreshold && (
            <span
              className="inline-flex items-center gap-1 atlas-text-caption text-[var(--color-error)]"
              role="status"
            >
              <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
              Flagged — review
            </span>
          )}
        </div>

        {/* Source (provenance) */}
        <div data-cell="source">
          {row.provenance ? (
            <ProvenanceBadge
              type={row.provenance}
              className="whitespace-nowrap"
            />
          ) : (
            <span className="atlas-text-caption text-muted-foreground">—</span>
          )}
        </div>
      </div>

      {/* Gap-fill explainer for AI-inferred fields */}
      {row.provenance === "ai_inferred" && (
        <div className="mt-[var(--space-3)]">
          <GapFillExplainer fieldLabel={row.label} grounding={grounding} />
        </div>
      )}
    </div>
  );
}
