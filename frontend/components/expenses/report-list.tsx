"use client";

import * as React from "react";
import { ChevronRight, AlertTriangle, Check } from "lucide-react";

import {
  AtlasButton,
  DataTable,
  type ColumnDef,
  type SortDirection,
} from "@/components/atlas";
import { ReportStatusBadge } from "./report-status-badge";
import { ReportListToolbar, type StatusFilter } from "./report-list-toolbar";
import { CreateReportDialog } from "./report-dialogs";
import { useExpenseReports } from "@/lib/query/expense-hooks";
import { useSession } from "@/lib/auth/session-provider";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import { displayReportStatus } from "@/lib/lifecycle/status";
import type { ReportListItem, ReportListParams } from "@/lib/types";

const PAGE_SIZE = 20;

/** Map a DataTable column key to an allow-listed backend sort field. */
const SORT_FIELD: Record<string, string> = {
  title: "title",
  total_amount: "total_amount",
  status: "status",
  submitted_at: "submitted_at",
};

function toSortParam(
  column: string,
  direction: SortDirection,
): string | undefined {
  const field = SORT_FIELD[column];
  if (!field || !direction) return undefined;
  return `${field}:${direction}`;
}

export interface ReportListProps {
  onSelectReport: (id: string) => void;
}

export function ReportList({ onSelectReport }: ReportListProps) {
  const [statusFilter, setStatusFilter] = React.useState<StatusFilter>("all");
  const [search, setSearch] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [sortColumn, setSortColumn] = React.useState<string | undefined>(
    "submitted_at",
  );
  const [sortDirection, setSortDirection] =
    React.useState<SortDirection>("desc");
  const [creating, setCreating] = React.useState(false);
  // Finance admins read every report but cannot create one (server: 403).
  const { hasPermission } = useSession();
  const canCreate = hasPermission("expense.create.own");

  // Reset to page 1 when the filter changes.
  const handleStatusFilter = (value: StatusFilter) => {
    setStatusFilter(value);
    setPage(1);
  };

  const params: ReportListParams = {
    page,
    size: PAGE_SIZE,
    status:
      statusFilter === "all" || statusFilter === "withdrawn"
        ? undefined
        : statusFilter,
    // Product gap 4: withdrawn reports are listed only through their filter.
    withdrawn: statusFilter === "withdrawn" ? true : undefined,
    sort: sortColumn ? toSortParam(sortColumn, sortDirection) : undefined,
  };

  const { data, isLoading, isError, refetch } = useExpenseReports(params);

  const handleSort = (column: string, direction: SortDirection) => {
    setSortColumn(direction ? column : undefined);
    setSortDirection(direction);
  };

  // Client-side title search (the list endpoint exposes no search param).
  const items = data?.items ?? [];
  const term = search.trim().toLowerCase();
  const filtered = term
    ? items.filter((r) => r.title.toLowerCase().includes(term))
    : items;

  // Rejected, or returned for changes (C7R/P12: request-changes puts the
  // report back to draft; its approval_status says it was returned).
  const returnedForChanges = (r: ReportListItem) =>
    r.status === "draft" &&
    !r.withdrawn_at &&
    r.approval_status === "changes_requested";
  const needsReview = (r: ReportListItem) =>
    r.status === "rejected" || returnedForChanges(r);

  const columns: ColumnDef<ReportListItem>[] = [
    {
      key: "title",
      header: "Report",
      sortable: true,
      render: (r) => (
        <div className="flex flex-col gap-0.5">
          <span className="font-medium text-foreground">{r.title}</span>
          {needsReview(r) && (
            <span className="inline-flex items-center gap-1 text-[var(--color-warning)] atlas-text-caption">
              <AlertTriangle className="h-3 w-3 shrink-0" aria-hidden="true" />
              {returnedForChanges(r)
                ? "Changes requested — edit and resubmit"
                : "Needs your review"}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "total_amount",
      header: "Amount",
      sortable: true,
      render: (r) => (
        <span className="font-mono text-foreground">
          {formatCurrency(r.total_amount, r.currency)}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      sortable: true,
      render: (r) => (
        <div className="flex flex-col items-start gap-0.5">
          <ReportStatusBadge status={displayReportStatus(r)} />
          {r.status === "reimbursed" && r.reimbursed_at && (
            <span
              data-testid={`report-reimbursed-${r.id}`}
              className="atlas-text-caption break-all text-muted-foreground"
            >
              Paid {formatDate(r.reimbursed_at)}
              {r.reimbursement_reference && (
                <>
                  {" · "}
                  <span className="font-mono">{r.reimbursement_reference}</span>
                </>
              )}
            </span>
          )}
        </div>
      ),
    },
    {
      key: "policy",
      header: "Policy",
      // Derived honestly from status: the list row carries no flag summary
      // (confirmed against the backend). The detail view is authoritative.
      render: (r) =>
        needsReview(r) ? (
          <span className="inline-flex items-center gap-1 text-[var(--color-warning)]">
            <AlertTriangle
              className="h-3.5 w-3.5 shrink-0"
              aria-hidden="true"
            />
            Review
          </span>
        ) : r.status === "approved" || r.status === "reimbursed" ? (
          <span className="inline-flex items-center gap-1 text-[var(--color-success)]">
            <Check className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            Cleared
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        ),
    },
    {
      key: "submitted_at",
      header: "Submitted",
      sortable: true,
      render: (r) => (
        <span className="text-muted-foreground">
          {formatDate(r.submitted_at)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      width: "w-[88px]",
      render: (r) => (
        <AtlasButton
          variant="ghost"
          size="sm"
          onClick={() => onSelectReport(r.id)}
          aria-label={`Open ${r.title}`}
        >
          View
          <ChevronRight className="h-4 w-4" aria-hidden="true" />
        </AtlasButton>
      ),
    },
  ];

  const tableState: "default" | "loading" | "empty" | "error" = isLoading
    ? "loading"
    : isError
      ? "error"
      : filtered.length === 0
        ? "empty"
        : "default";

  const isFiltering = term.length > 0 || statusFilter !== "all";

  return (
    <div className="flex flex-col gap-4">
      <ReportListToolbar
        statusFilter={statusFilter}
        onStatusFilterChange={handleStatusFilter}
        search={search}
        onSearchChange={setSearch}
        onNewReport={canCreate ? () => setCreating(true) : undefined}
      />

      {creating && (
        <CreateReportDialog
          onOpenChange={setCreating}
          onCreated={(report) => onSelectReport(report.id)}
        />
      )}

      <DataTable<ReportListItem>
        columns={columns}
        data={filtered}
        getRowKey={(r) => r.id}
        state={tableState}
        sortColumn={sortColumn}
        sortDirection={sortDirection}
        onSort={handleSort}
        pagination={
          data && !isFiltering
            ? { page: data.page, pageSize: data.size, total: data.total }
            : undefined
        }
        onPageChange={setPage}
        emptyState={{
          heading: isFiltering ? "No matching reports" : "No reports yet",
          subtext: isFiltering
            ? "Try a different status filter or search term."
            : "Upload a receipt or create a report with “New report”.",
        }}
        errorState={{
          heading: "Couldn’t load your reports",
          subtext: "Something went wrong fetching expense reports.",
          onRetry: () => refetch(),
        }}
      />
    </div>
  );
}
