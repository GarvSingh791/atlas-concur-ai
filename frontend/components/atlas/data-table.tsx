"use client";

import * as React from "react";
import {
  ChevronUp,
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  Filter,
} from "lucide-react";
import { EmptyState } from "./empty-state";
import { ErrorState } from "./error-state";
import { cn } from "@/lib/utils";

/**
 * Atlas Data Table — sortable, filterable, paginated, expandable, selectable.
 *
 * Spec: UI_UX.md → Tables
 * Header bg --surface-table-header, stripe --surface-table-stripe even rows,
 * hover --color-primary-50, 48px row height, 12px 16px cell padding.
 */

export interface ColumnDef<T> {
  /** Unique column key. */
  key: string;
  /** Column header label. */
  header: string;
  /** Render cell content. */
  render: (row: T) => React.ReactNode;
  /** Whether this column is sortable. Default: false. */
  sortable?: boolean;
  /** Whether to show a filter icon. Default: false. */
  filterable?: boolean;
  /** Column width class (e.g. "w-[120px]"). */
  width?: string;
}

export type SortDirection = "asc" | "desc" | null;

export interface DataTableProps<T> {
  /** Column definitions. */
  columns: ColumnDef<T>[];
  /** Row data. */
  data: T[];
  /** A function that returns a unique key for each row. */
  getRowKey: (row: T) => string;
  /** Current table state. Default: "default". */
  state?: "default" | "loading" | "empty" | "error";
  /** Enable row selection checkboxes. */
  selectable?: boolean;
  /** Currently selected row keys. */
  selectedKeys?: Set<string>;
  /** Selection change handler. */
  onSelectionChange?: (keys: Set<string>) => void;
  /** Enable expandable rows. */
  expandable?: boolean;
  /** Render the expanded row content. */
  renderExpanded?: (row: T) => React.ReactNode;
  /** Current sort column. */
  sortColumn?: string;
  /** Current sort direction. */
  sortDirection?: SortDirection;
  /** Sort change handler. */
  onSort?: (column: string, direction: SortDirection) => void;
  /** Pagination info. */
  pagination?: {
    page: number;
    pageSize: number;
    total: number;
  };
  /** Page change handler. */
  onPageChange?: (page: number) => void;
  /** Custom empty state config. */
  emptyState?: {
    heading: string;
    subtext: string;
    ctaLabel?: string;
    onCta?: () => void;
  };
  /** Custom error state config. */
  errorState?: {
    heading: string;
    subtext: string;
    onRetry?: () => void;
  };
  className?: string;
}

function LoadingRows({
  columns,
  rows = 5,
}: {
  columns: number;
  rows?: number;
}) {
  return (
    <>
      {Array.from({ length: rows }, (_, i) => (
        <tr key={i} className="border-b border-[#F1F5F9]">
          {Array.from({ length: columns }, (_, j) => (
            <td key={j} className="px-3 py-3 md:px-4">
              <div className="atlas-shimmer h-4 w-full max-w-[120px] rounded" />
            </td>
          ))}
        </tr>
      ))}
    </>
  );
}

export function DataTable<T>({
  columns,
  data,
  getRowKey,
  state = "default",
  selectable = false,
  selectedKeys = new Set(),
  onSelectionChange,
  expandable = false,
  renderExpanded,
  sortColumn,
  sortDirection,
  onSort,
  pagination,
  onPageChange,
  emptyState: emptyConfig,
  errorState: errorConfig,
  className,
}: DataTableProps<T>) {
  const [expandedKeys, setExpandedKeys] = React.useState<Set<string>>(
    new Set(),
  );

  const totalColumns =
    columns.length + (selectable ? 1 : 0) + (expandable ? 1 : 0);

  const handleSort = (column: string) => {
    if (!onSort) return;
    let newDirection: SortDirection = "asc";
    if (sortColumn === column) {
      if (sortDirection === "asc") newDirection = "desc";
      else if (sortDirection === "desc") newDirection = null;
    }
    onSort(column, newDirection);
  };

  const handleSelectAll = () => {
    if (!onSelectionChange) return;
    if (selectedKeys.size === data.length) {
      onSelectionChange(new Set());
    } else {
      onSelectionChange(new Set(data.map(getRowKey)));
    }
  };

  const handleSelectRow = (key: string) => {
    if (!onSelectionChange) return;
    const next = new Set(selectedKeys);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onSelectionChange(next);
  };

  const toggleExpand = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  // Empty / Error states
  if (state === "empty") {
    return (
      <div
        className={cn(
          "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
          className,
        )}
      >
        <EmptyState
          heading={emptyConfig?.heading ?? "No data found"}
          subtext={emptyConfig?.subtext ?? "There are no items to display."}
          ctaLabel={emptyConfig?.ctaLabel}
          onCta={emptyConfig?.onCta}
        />
      </div>
    );
  }

  if (state === "error") {
    return (
      <div
        className={cn(
          "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
          className,
        )}
      >
        <ErrorState
          heading={errorConfig?.heading ?? "Unable to load data"}
          subtext={
            errorConfig?.subtext ??
            "We're having trouble connecting. Your data is safe."
          }
          onRetry={errorConfig?.onRetry}
        />
      </div>
    );
  }

  // Pagination info
  const paginationInfo = pagination
    ? (() => {
        const start = (pagination.page - 1) * pagination.pageSize + 1;
        const end = Math.min(
          pagination.page * pagination.pageSize,
          pagination.total,
        );
        const totalPages = Math.ceil(pagination.total / pagination.pageSize);
        return { start, end, totalPages };
      })()
    : null;

  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)] overflow-hidden",
        className,
      )}
    >
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            <tr className="bg-[var(--surface-table-header)] border-b border-[#E2E8F0]">
              {/* Select-all checkbox */}
              {selectable && (
                <th className="w-10 px-3 py-3 md:px-4">
                  <input
                    type="checkbox"
                    checked={
                      data.length > 0 && selectedKeys.size === data.length
                    }
                    onChange={handleSelectAll}
                    className="h-4 w-4 rounded border-[#CBD5E1] text-[var(--color-primary-600)] focus:ring-[var(--color-primary-200)]"
                    aria-label="Select all rows"
                  />
                </th>
              )}
              {/* Expand column */}
              {expandable && <th className="w-10 px-3 py-3 md:px-4" />}
              {/* Data columns */}
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={cn(
                    "px-3 py-3 md:px-4 text-left",
                    "atlas-text-overline text-[var(--table-header-color)]",
                    col.sortable && "cursor-pointer select-none",
                    col.width,
                  )}
                  onClick={col.sortable ? () => handleSort(col.key) : undefined}
                  aria-sort={
                    sortColumn === col.key && sortDirection
                      ? sortDirection === "asc"
                        ? "ascending"
                        : "descending"
                      : undefined
                  }
                >
                  <div className="flex items-center gap-1">
                    {col.header}
                    {col.sortable && (
                      <span className="inline-flex shrink-0">
                        {sortColumn === col.key ? (
                          sortDirection === "asc" ? (
                            <ChevronUp className="h-3.5 w-3.5 text-[var(--color-primary-600)]" />
                          ) : sortDirection === "desc" ? (
                            <ChevronDown className="h-3.5 w-3.5 text-[var(--color-primary-600)]" />
                          ) : (
                            <ChevronsUpDown className="h-3.5 w-3.5 text-[#94A3B8]" />
                          )
                        ) : (
                          <ChevronsUpDown className="h-3.5 w-3.5 text-[#94A3B8]" />
                        )}
                      </span>
                    )}
                    {col.filterable && (
                      <Filter
                        className="h-3 w-3 text-[#94A3B8] ml-0.5 shrink-0"
                        aria-hidden="true"
                      />
                    )}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {state === "loading" ? (
              <LoadingRows columns={totalColumns} />
            ) : (
              data.map((row, rowIndex) => {
                const key = getRowKey(row);
                const isExpanded = expandedKeys.has(key);
                const isSelected = selectedKeys.has(key);

                return (
                  <React.Fragment key={key}>
                    <tr
                      className={cn(
                        "border-b border-[#F1F5F9] transition-colors duration-atlas-fast",
                        "h-[var(--table-row-height)]",
                        rowIndex % 2 === 1 &&
                          "bg-[var(--surface-table-stripe)]",
                        "hover:bg-[var(--color-primary-50)]",
                        isSelected && "bg-[var(--color-primary-50)]",
                      )}
                    >
                      {/* Selection checkbox */}
                      {selectable && (
                        <td className="px-3 py-3 md:px-4">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => handleSelectRow(key)}
                            className="h-4 w-4 rounded border-[#CBD5E1] text-[var(--color-primary-600)] focus:ring-[var(--color-primary-200)]"
                            aria-label={`Select row ${key}`}
                          />
                        </td>
                      )}
                      {/* Expand toggle */}
                      {expandable && (
                        <td className="px-3 py-3 md:px-4">
                          <button
                            type="button"
                            onClick={() => toggleExpand(key)}
                            className={cn(
                              "p-1 rounded transition-transform duration-atlas-fast",
                              "hover:bg-[#F1F5F9]",
                              "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)]",
                              isExpanded && "rotate-90",
                            )}
                            aria-expanded={isExpanded}
                            aria-label="Expand row"
                          >
                            <ChevronRight className="h-4 w-4 text-[#64748B]" />
                          </button>
                        </td>
                      )}
                      {/* Data cells */}
                      {columns.map((col) => (
                        <td
                          key={col.key}
                          className={cn(
                            "px-3 py-3 md:px-4",
                            "atlas-text-body text-[#334155]",
                            col.width,
                          )}
                        >
                          {col.render(row)}
                        </td>
                      ))}
                    </tr>
                    {/* Expanded content */}
                    {expandable && isExpanded && renderExpanded && (
                      <tr>
                        <td
                          colSpan={totalColumns}
                          className="bg-[#F8FAFC] px-6 py-4 border-b border-[#F1F5F9]"
                        >
                          {renderExpanded(row)}
                        </td>
                      </tr>
                    )}
                  </React.Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {pagination && paginationInfo && (
        <div className="flex items-center justify-between px-4 py-3 border-t border-[#E2E8F0]">
          <span className="atlas-text-caption text-[#64748B]">
            Showing {paginationInfo.start}–{paginationInfo.end} of{" "}
            {pagination.total}
          </span>
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => onPageChange && onPageChange(pagination.page - 1)}
              disabled={pagination.page <= 1}
              className={cn(
                "px-3 py-1.5 rounded-atlas-md text-[length:var(--text-caption-size)] font-medium",
                "transition-colors duration-atlas-fast",
                "hover:bg-[#F1F5F9]",
                "disabled:opacity-50 disabled:pointer-events-none",
                "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)]",
              )}
              aria-label="Previous page"
            >
              Previous
            </button>
            {Array.from(
              { length: Math.min(paginationInfo.totalPages, 5) },
              (_, i) => i + 1,
            ).map((pageNum) => (
              <button
                key={pageNum}
                type="button"
                onClick={() => onPageChange && onPageChange(pageNum)}
                className={cn(
                  "h-8 w-8 rounded-atlas-md text-[length:var(--text-caption-size)] font-medium",
                  "transition-colors duration-atlas-fast",
                  "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)]",
                  pageNum === pagination.page
                    ? "bg-[var(--color-primary-600)] text-white"
                    : "text-[#64748B] hover:bg-[#F1F5F9]",
                )}
                aria-label={`Page ${pageNum}`}
                aria-current={pageNum === pagination.page ? "page" : undefined}
              >
                {pageNum}
              </button>
            ))}
            <button
              type="button"
              onClick={() => onPageChange && onPageChange(pagination.page + 1)}
              disabled={pagination.page >= paginationInfo.totalPages}
              className={cn(
                "px-3 py-1.5 rounded-atlas-md text-[length:var(--text-caption-size)] font-medium",
                "transition-colors duration-atlas-fast",
                "hover:bg-[#F1F5F9]",
                "disabled:opacity-50 disabled:pointer-events-none",
                "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)]",
              )}
              aria-label="Next page"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
