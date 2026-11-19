"use client";

import * as React from "react";

import {
  DataTable,
  type ColumnDef,
  type SortDirection,
} from "@/components/atlas";
import { ExportCsvButton } from "@/components/exports/export-csv-button";
import { useAuditLog } from "@/lib/query/audit-hooks";
import { isApiError } from "@/lib/types/api";
import type { AuditLogItem } from "@/lib/types/audit";
import {
  AuditFilters,
  EMPTY_AUDIT_FILTERS,
  type AuditFilterValues,
} from "./audit-filters";

export const AUDIT_PAGE_SIZE = 20;

/** Locale timestamp for an audit row (date + time; the log is time-ordered). */
export function formatAuditTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function StateBlock({
  label,
  state,
}: {
  label: string;
  state: Record<string, unknown> | null;
}) {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <span className="atlas-text-overline text-muted-foreground">{label}</span>
      {state ? (
        <pre className="max-h-64 overflow-auto rounded-atlas-md border border-[#E2E8F0] bg-white p-3 font-mono text-xs text-[#334155]">
          {JSON.stringify(state, null, 2)}
        </pre>
      ) : (
        <span className="atlas-text-caption text-muted-foreground">—</span>
      )}
    </div>
  );
}

/**
 * Audit trail — paginated, filterable, org-scoped read of GET /audit.
 *
 * Server truth only: the list is ordered, filtered and paginated server-side
 * (exact-match `action` / `entity_type`; sort on `created_at`). Rows expand to
 * the recorded before/after snapshots. Read-only — nothing here writes.
 */
export function AuditTrail() {
  const [filters, setFilters] =
    React.useState<AuditFilterValues>(EMPTY_AUDIT_FILTERS);
  const [page, setPage] = React.useState(1);
  const [sortDirection, setSortDirection] = React.useState<"asc" | "desc">(
    "desc",
  );

  const { data, isLoading, isError, error, refetch } = useAuditLog({
    page,
    size: AUDIT_PAGE_SIZE,
    sort: `created_at:${sortDirection}`,
    action: filters.action,
    entity_type: filters.entity_type,
  });

  const applyFilters = (next: AuditFilterValues) => {
    setFilters(next);
    setPage(1);
  };

  // Only created_at is sortable; toggle between newest- and oldest-first
  // (the table's third "unsorted" step maps back to the opposite direction).
  const handleSort = (_column: string, direction: SortDirection) => {
    setSortDirection(direction ?? (sortDirection === "desc" ? "asc" : "desc"));
    setPage(1);
  };

  const items = data?.items ?? [];
  const isFiltering = filters.action !== "" || filters.entity_type !== "";
  const forbidden = isApiError(error) && error.status === 403;

  const columns: ColumnDef<AuditLogItem>[] = [
    {
      key: "created_at",
      header: "Time",
      sortable: true,
      render: (r) => (
        <time dateTime={r.created_at} className="whitespace-nowrap">
          {formatAuditTimestamp(r.created_at)}
        </time>
      ),
    },
    {
      key: "actor",
      header: "Actor",
      render: (r) =>
        r.actor_name ? (
          <span className="text-foreground">{r.actor_name}</span>
        ) : (
          <span className="italic text-muted-foreground">System</span>
        ),
    },
    {
      key: "action",
      header: "Action",
      render: (r) => (
        <span className="font-mono text-foreground">{r.action}</span>
      ),
    },
    {
      key: "entity_type",
      header: "Entity type",
      render: (r) => r.entity_type,
    },
    {
      key: "entity_id",
      header: "Entity ID",
      render: (r) => (
        <span className="font-mono text-muted-foreground" title={r.entity_id}>
          {r.entity_id.slice(0, 8)}…
        </span>
      ),
    },
  ];

  const tableState: "default" | "loading" | "empty" | "error" = isLoading
    ? "loading"
    : isError
      ? "error"
      : items.length === 0
        ? "empty"
        : "default";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <AuditFilters value={filters} onApply={applyFilters} />
        {/* Product gap 8: the same filters + order as the table (finance-only). */}
        <ExportCsvButton
          path="/audit/export.csv"
          params={{
            action: filters.action,
            entity_type: filters.entity_type,
            sort: `created_at:${sortDirection}`,
          }}
          filenameStem="audit-log"
        />
      </div>

      <DataTable<AuditLogItem>
        columns={columns}
        data={items}
        getRowKey={(r) => r.id}
        state={tableState}
        sortColumn="created_at"
        sortDirection={sortDirection}
        onSort={handleSort}
        expandable
        renderExpanded={(r) => (
          <div className="flex flex-col gap-3 md:flex-row">
            <StateBlock label="Before" state={r.before_state} />
            <StateBlock label="After" state={r.after_state} />
          </div>
        )}
        pagination={
          data && data.total > 0
            ? { page: data.page, pageSize: data.size, total: data.total }
            : undefined
        }
        onPageChange={setPage}
        emptyState={{
          heading: isFiltering
            ? "No matching audit events"
            : "No audit events yet",
          subtext: isFiltering
            ? "No events match these filters. Filters are exact matches."
            : "State changes across your organization will be recorded here.",
        }}
        errorState={
          forbidden
            ? {
                heading: "Audit trail unavailable",
                subtext:
                  "The audit trail is available to finance administrators.",
              }
            : {
                heading: "Couldn’t load the audit trail",
                subtext: "Something went wrong fetching audit events.",
                onRetry: () => refetch(),
              }
        }
      />
    </div>
  );
}
