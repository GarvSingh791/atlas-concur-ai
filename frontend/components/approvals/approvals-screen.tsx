"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { useSession } from "@/lib/auth/session-provider";
import {
  activeFilterCount,
  dateRangeError,
  filtersFromSearchParams,
  filtersToQueryString,
  type ApprovalFilters,
} from "@/lib/approvals/filter-params";
import { useApproval, useApprovals } from "@/lib/query/approval-hooks";
import {
  useApprovalDepartments,
  useExceptionTypes,
} from "@/lib/query/approval-filter-hooks";
import type { ApprovalListParams } from "@/lib/types/approvals";
import { cn } from "@/lib/utils";
import { ApprovalFilterBar } from "./approval-filter-bar";
import { ApprovalQueue } from "./approval-queue";
import { DecisionPanel } from "./decision-panel";

const QUEUE_SIZE = 100;

/**
 * Approval Dashboard — /(app)/approvals · P5-T8 (US-M1/M2/M3-scope/F2).
 *
 * Split layout: role-scoped queue (left) + decision panel (right). Renders the
 * server's truth only — the scoped queue + the Approval Agent's risk_summary —
 * and triggers decisions the server commits + audits. The queue is server-scoped
 * (manager → reports, finance → org); the screen never client-filters for
 * authority. Mobile stacks: queue OR panel, toggled by selection.
 */
export function ApprovalsScreen() {
  const { hasPermission, user } = useSession();
  const canDecide = hasPermission("approval.decide.reports");
  // Finance-only export (the server enforces it: approval.read + expense.read.org).
  const canExport = hasPermission("expense.read.org");

  // Product gap 6: server-side filters live in the URL (linkable views).
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters = React.useMemo(
    () => filtersFromSearchParams(searchParams),
    [searchParams],
  );
  const setFilters = React.useCallback(
    (next: ApprovalFilters) => {
      const qs = filtersToQueryString(next);
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [router, pathname],
  );
  // One scoped fetch; the queue splits it into the active queue + auto lane.
  // filtersFromSearchParams only keeps enum-valid status / type values.
  // QA round: an inverted date range is caught here (inline message in the
  // filter bar) and never sent — the server would only answer 400.
  const rangeError = dateRangeError(filters);
  // Follow-up: the exception-type counts follow the other active filters.
  const exceptionTypes = useExceptionTypes(filters, rangeError === null);
  const departments = useApprovalDepartments();
  const list = useApprovals(
    {
      size: QUEUE_SIZE,
      ...filters,
    } as ApprovalListParams,
    { enabled: rangeError === null },
  );
  // Product gap 3: "/approvals?approval=<id>" (notifications, global search)
  // preselects that approval; the detail fetch stays server-scoped (404 if not
  // visible to the caller).
  const linkedId = searchParams?.get("approval") ?? null;
  const [selectedId, setSelectedId] = React.useState<string | null>(linkedId);
  const [viewedIds, setViewedIds] = React.useState<Set<string>>(() =>
    linkedId ? new Set([linkedId]) : new Set(),
  );
  const detail = useApproval(selectedId);

  const handleSelect = React.useCallback((id: string) => {
    setSelectedId(id);
    setViewedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  }, []);

  React.useEffect(() => {
    if (linkedId) handleSelect(linkedId);
  }, [linkedId, handleSelect]);

  const items = list.data?.items ?? [];

  return (
    <div className="mx-auto flex min-h-[calc(100vh-8rem)] w-full max-w-6xl flex-col gap-4 p-4 sm:p-6 lg:h-[calc(100vh-8rem)]">
      <header className="flex flex-col gap-1">
        <h1 className="atlas-text-display text-foreground">Approvals</h1>
        <p className="atlas-text-body text-muted-foreground">
          Review and decide on items routed to you.
        </p>
      </header>

      <ApprovalFilterBar
        value={filters}
        onChange={setFilters}
        exceptionTypes={exceptionTypes.data ?? []}
        departments={departments.data ?? []}
        total={list.data?.total ?? null}
        loaded={items.length}
        isFetching={list.isFetching}
        canExport={canExport}
        rangeError={rangeError}
      />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,380px)_minmax(0,1fr)]">
        <div
          className={cn(
            "overflow-hidden rounded-atlas-lg border border-border bg-[var(--surface-card)]",
            selectedId ? "hidden lg:block" : "block",
          )}
        >
          <ApprovalQueue
            items={items}
            isLoading={list.isLoading}
            isError={list.isError}
            error={list.error}
            blockedReason={rangeError}
            onRetry={() => list.refetch()}
            selectedId={selectedId}
            viewedIds={viewedIds}
            onSelect={handleSelect}
            serverFiltered={activeFilterCount(filters) > 0}
          />
        </div>
        <div
          className={cn(
            "overflow-hidden rounded-atlas-lg border border-border bg-[var(--surface-card)]",
            selectedId ? "block" : "hidden lg:block",
          )}
        >
          <DecisionPanel
            approvalId={selectedId}
            detail={detail.data ?? null}
            isLoading={Boolean(selectedId) && detail.isLoading}
            isError={detail.isError}
            onRetry={() => detail.refetch()}
            onBack={() => setSelectedId(null)}
            canDecide={canDecide}
            currentUserId={user?.id ?? null}
          />
        </div>
      </div>
    </div>
  );
}
