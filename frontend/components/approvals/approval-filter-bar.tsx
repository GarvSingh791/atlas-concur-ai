"use client";

import * as React from "react";
import { X } from "lucide-react";

import { AtlasButton } from "@/components/atlas";
import { ExportCsvButton } from "@/components/exports/export-csv-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  APPROVAL_STATUS_OPTIONS,
  SUBJECT_TYPE_OPTIONS,
  activeFilterCount,
  withFilter,
  type ApprovalFilterKey,
  type ApprovalFilters,
} from "@/lib/approvals/filter-params";
import type { ExceptionTypeOption } from "@/lib/types/approval-filters";

const SELECT_CLASS =
  "h-9 w-full rounded-atlas-md border border-border bg-background px-2 atlas-text-body focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary-600)]";

export interface ApprovalFilterBarProps {
  value: ApprovalFilters;
  onChange: (next: ApprovalFilters) => void;
  exceptionTypes: ExceptionTypeOption[];
  departments: string[];
  /** Server total for the current filters (null while unknown). */
  total: number | null;
  /** Rows actually loaded (the queue shows at most this many). */
  loaded: number;
  isFetching: boolean;
  /** Finance only — the export endpoint is finance-only server-side. */
  canExport: boolean;
  /** QA round: inline message for an inverted submitted-date range. */
  rangeError?: string | null;
}

/**
 * Server-side approval filters (Product gap 6, US-F2 AC1): status, type,
 * exception type, department, submitted date range. Every change rewrites the
 * URL (linkable views); "Clear all" resets it. Exception types list how many
 * approvals in YOUR scope carry each (server counts).
 */
export function ApprovalFilterBar({
  value,
  onChange,
  exceptionTypes,
  departments,
  total,
  loaded,
  isFetching,
  canExport,
  rangeError = null,
}: ApprovalFilterBarProps) {
  const active = activeFilterCount(value);
  const set =
    (key: ApprovalFilterKey) =>
    (e: React.ChangeEvent<HTMLSelectElement | HTMLInputElement>) =>
      onChange(withFilter(value, key, e.target.value));

  // Keep a selected value visible even when it has no option (e.g. a linked
  // URL naming a department that no longer exists).
  const departmentOptions =
    value.department && !departments.includes(value.department)
      ? [...departments, value.department]
      : departments;

  return (
    <section
      aria-label="Filter approvals"
      data-testid="approval-filters"
      className="flex flex-col gap-3 rounded-atlas-lg border border-border bg-[var(--surface-card)] p-[var(--space-4)]"
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-6">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-status">Status</Label>
          <select
            id="af-status"
            className={SELECT_CLASS}
            value={value.current_status ?? ""}
            onChange={set("current_status")}
          >
            <option value="">Any status</option>
            {APPROVAL_STATUS_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-type">Type</Label>
          <select
            id="af-type"
            className={SELECT_CLASS}
            value={value.subject_type ?? ""}
            onChange={set("subject_type")}
          >
            <option value="">Trips &amp; reports</option>
            {SUBJECT_TYPE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-exception">Exception type</Label>
          <select
            id="af-exception"
            className={SELECT_CLASS}
            value={value.exception_type ?? ""}
            onChange={set("exception_type")}
          >
            <option value="">Any exception</option>
            {exceptionTypes.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label} ({o.count})
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-department">Department</Label>
          <select
            id="af-department"
            className={SELECT_CLASS}
            value={value.department ?? ""}
            onChange={set("department")}
          >
            <option value="">All departments</option>
            {departmentOptions.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-from">Submitted from</Label>
          <Input
            id="af-from"
            type="date"
            value={value.submitted_from ?? ""}
            max={value.submitted_to || undefined}
            onChange={set("submitted_from")}
            aria-invalid={rangeError ? true : undefined}
            aria-describedby={rangeError ? "af-range-error" : undefined}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="af-to">Submitted to</Label>
          <Input
            id="af-to"
            type="date"
            value={value.submitted_to ?? ""}
            min={value.submitted_from || undefined}
            onChange={set("submitted_to")}
            aria-invalid={rangeError ? true : undefined}
            aria-describedby={rangeError ? "af-range-error" : undefined}
          />
        </div>
      </div>

      {rangeError && (
        <p
          id="af-range-error"
          role="alert"
          className="atlas-text-caption text-[var(--color-error)]"
          data-testid="approval-date-range-error"
        >
          {rangeError}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p
          className="atlas-text-caption text-muted-foreground"
          aria-live="polite"
          data-testid="approval-result-count"
        >
          {rangeError
            ? ""
            : isFetching && total === null
              ? "Loading…"
              : total === null
                ? ""
                : `${total} ${total === 1 ? "result" : "results"}` +
                  (total > loaded ? ` · showing the first ${loaded}` : "") +
                  (active > 0
                    ? ` · ${active} ${active === 1 ? "filter" : "filters"} applied`
                    : "")}
        </p>
        <div className="flex items-center gap-2">
          {active > 0 && (
            <AtlasButton
              type="button"
              variant="ghost"
              size="md"
              onClick={() => onChange({})}
              data-testid="approval-filters-clear"
            >
              <X className="h-4 w-4" aria-hidden="true" />
              Clear all
            </AtlasButton>
          )}
          {canExport && (
            <ExportCsvButton
              path="/approvals/export.csv"
              params={value}
              filenameStem="approvals"
            />
          )}
        </div>
      </div>
    </section>
  );
}
