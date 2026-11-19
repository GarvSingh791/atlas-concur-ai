"use client";

import * as React from "react";
import Link from "next/link";
import { Search, Users } from "lucide-react";

import {
  DataTable,
  EmptyState,
  type ColumnDef,
  type SortDirection,
} from "@/components/atlas";
import { InactiveMarker } from "@/components/team/inactive-marker";
import { Label } from "@/components/ui/label";
import { formatMoney } from "@/lib/approvals/format";
import { useSession } from "@/lib/auth/session-provider";
import { useTeamMembers } from "@/lib/query/team-hooks";
import { isApiError } from "@/lib/types/api";
import type { TeamMemberSummary, TeamSortKey } from "@/lib/types/team";

export const TEAM_PAGE_SIZE = 20;

const SORT_OPTIONS: { value: string; label: string }[] = [
  { value: "name", label: "Name (A–Z)" },
  { value: "-spend_90d", label: "Spend, last 90 days" },
  { value: "-spend_mtd", label: "Spend, this month" },
  { value: "-pending_approvals", label: "Pending approvals" },
  { value: "-open_trips", label: "Open trips" },
];

const SORTABLE: TeamSortKey[] = [
  "name",
  "department",
  "open_trips",
  "pending_approvals",
  "spend_mtd",
  "spend_90d",
];

/** "-spend_90d" → ["spend_90d", "desc"]. */
export function parseTeamSort(sort: string): [TeamSortKey, "asc" | "desc"] {
  const desc = sort.startsWith("-");
  const key = (desc ? sort.slice(1) : sort) as TeamSortKey;
  return [SORTABLE.includes(key) ? key : "name", desc ? "desc" : "asc"];
}

/** The open (in-flight) report count: submitted reports awaiting a decision. */
function openReports(m: TeamMemberSummary): number {
  return m.reports_by_status.submitted ?? 0;
}

/**
 * Team (Product gap 5, PRD US-M3) — a manager's direct reports with their
 * travel & expense posture. Server truth only: the team, the counts and the
 * spend are computed and scoped server-side (manager → direct reports,
 * finance → the organization). Each name opens the read-only drill-down.
 */
export function TeamScreen() {
  const { role } = useSession();
  const [query, setQuery] = React.useState("");
  const [debounced, setDebounced] = React.useState("");
  const [sort, setSort] = React.useState("name");
  const [page, setPage] = React.useState(1);

  React.useEffect(() => {
    const t = setTimeout(() => {
      setDebounced(query.trim());
      setPage(1);
    }, 250);
    return () => clearTimeout(t);
  }, [query]);

  const { data, isLoading, isError, error, refetch } = useTeamMembers({
    page,
    size: TEAM_PAGE_SIZE,
    q: debounced,
    sort,
  });
  const items = data?.items ?? [];
  const [sortColumn, sortDirection] = parseTeamSort(sort);
  const isOrgWide = role === "finance_admin";

  const handleSort = (column: string, direction: SortDirection) => {
    if (!direction) {
      setSort("name");
    } else {
      setSort(direction === "desc" ? `-${column}` : column);
    }
    setPage(1);
  };

  const columns: ColumnDef<TeamMemberSummary>[] = [
    {
      key: "name",
      header: "Name",
      sortable: true,
      render: (m) => (
        <span className="inline-flex flex-wrap items-center gap-2">
          <Link
            href={`/team/${m.id}`}
            className="font-medium text-[var(--color-primary-600)] hover:underline"
            data-testid="team-member-link"
          >
            {m.name}
          </Link>
          {m.status === "inactive" && <InactiveMarker />}
        </span>
      ),
    },
    {
      key: "department",
      header: "Department",
      sortable: true,
      render: (m) =>
        m.department ?? <span className="text-muted-foreground">—</span>,
    },
    {
      key: "open_trips",
      header: "Open trips",
      sortable: true,
      render: (m) => m.open_trips,
    },
    {
      key: "reports",
      header: "Reports in review",
      render: (m) => openReports(m),
    },
    {
      key: "pending_approvals",
      header: "Pending approvals",
      sortable: true,
      render: (m) => m.pending_approvals,
    },
    {
      key: "spend_mtd",
      header: "Spend (month)",
      sortable: true,
      render: (m) => formatMoney(m.spend_mtd, m.currency),
    },
    {
      key: "spend_90d",
      header: "Spend (90 days)",
      sortable: true,
      render: (m) => (
        <span
          title={
            m.other_currency_reports > 0
              ? `${m.other_currency_reports} report(s) in another currency not included`
              : undefined
          }
        >
          {formatMoney(m.spend_90d, m.currency)}
          {m.other_currency_reports > 0 ? " *" : ""}
        </span>
      ),
    },
  ];

  const forbidden = isApiError(error) && error.status === 403;
  const tableState: "default" | "loading" | "empty" | "error" = isLoading
    ? "loading"
    : isError
      ? "error"
      : items.length === 0
        ? "empty"
        : "default";

  if (tableState === "empty" && !debounced) {
    return (
      <div className="flex flex-col gap-4">
        <EmptyState
          icon={Users}
          heading={isOrgWide ? "No people yet" : "No direct reports yet"}
          subtext={
            isOrgWide
              ? "People in your organization will appear here."
              : "When people report to you, their trips, reports and spend appear here."
          }
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4" data-testid="team-screen">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="relative flex-1">
          <Label htmlFor="team-search" className="sr-only">
            Search team
          </Label>
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <input
            id="team-search"
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            maxLength={100}
            placeholder="Search by name or department"
            className="w-full rounded-atlas-md border border-border bg-background py-1.5 pl-8 pr-3 atlas-text-body focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary-600)]"
          />
        </div>
        <div className="flex flex-col gap-1.5 sm:w-56">
          <Label htmlFor="team-sort">Sort by</Label>
          <select
            id="team-sort"
            value={SORT_OPTIONS.some((o) => o.value === sort) ? sort : ""}
            onChange={(e) => {
              setSort(e.target.value || "name");
              setPage(1);
            }}
            className="h-9 rounded-atlas-md border border-border bg-background px-2 atlas-text-body"
          >
            {!SORT_OPTIONS.some((o) => o.value === sort) && (
              <option value="">Custom</option>
            )}
            {SORT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {data && (
        <p
          className="atlas-text-caption text-muted-foreground"
          aria-live="polite"
        >
          {data.total} {data.total === 1 ? "person" : "people"}
        </p>
      )}

      <DataTable<TeamMemberSummary>
        columns={columns}
        data={items}
        getRowKey={(m) => m.id}
        state={tableState}
        sortColumn={sortColumn}
        sortDirection={sortDirection}
        onSort={handleSort}
        pagination={
          data && data.total > 0
            ? { page: data.page, pageSize: data.size, total: data.total }
            : undefined
        }
        onPageChange={setPage}
        emptyState={{
          heading: "No matching people",
          subtext: "Nobody on your team matches this search.",
        }}
        errorState={
          forbidden
            ? {
                heading: "Team view unavailable",
                subtext: "The team view is available to managers and finance.",
              }
            : {
                heading: "Couldn’t load your team",
                subtext: "Something went wrong. Please try again.",
                onRetry: () => refetch(),
              }
        }
      />
      <p className="atlas-text-caption text-muted-foreground">
        Spend counts approved and reimbursed reports in your organization’s base
        currency. * Some reports in another currency are not included.
      </p>
    </div>
  );
}
