"use client";

import * as React from "react";
import { Inbox, Search } from "lucide-react";

import { ErrorState, LoadingState, StatusBadge } from "@/components/atlas";
import { EmptyState } from "@/components/atlas";
import { cn } from "@/lib/utils";
import {
  formatMoney,
  riskBadgeVariant,
  statusBadgeVariant,
  statusLabel,
} from "@/lib/approvals/format";
import { validationErrorMessage } from "@/lib/types/api";
import type { ApprovalListItem, RiskLevel } from "@/lib/types/approvals";
import { requiredGradeLabel } from "@/lib/approvals/grade";
import { AutoApprovedLane } from "./auto-approved-lane";

/**
 * Role-scoped approval queue (US-M1, US-F2 filters). The list is ALREADY scoped
 * by the server (scope_query) — the search + risk pills below are DISPLAY-only
 * refinements over that authorized set, never authority filtering. Auto-approved
 * items are split into the collapsed lane; everything else is the active queue.
 */

interface ApprovalQueueProps {
  items: ApprovalListItem[];
  isLoading: boolean;
  isError: boolean;
  /** The list query's error — a 400 shows the server's validation message. */
  error?: unknown;
  /**
   * QA round: the filters are invalid client-side (e.g. an inverted date
   * range) so no request was sent — show this instead of the list.
   */
  blockedReason?: string | null;
  onRetry: () => void;
  selectedId: string | null;
  viewedIds: Set<string>;
  onSelect: (id: string) => void;
  /** Server-side filters (approval-filter-bar) narrowed this list. */
  serverFiltered?: boolean;
}

type RiskFilter = "all" | RiskLevel;

const RISK_FILTERS: RiskFilter[] = ["all", "high", "medium", "low"];

export function ApprovalQueue({
  items,
  isLoading,
  isError,
  error,
  blockedReason = null,
  onRetry,
  selectedId,
  viewedIds,
  onSelect,
  serverFiltered = false,
}: ApprovalQueueProps) {
  const [search, setSearch] = React.useState("");
  const [riskFilter, setRiskFilter] = React.useState<RiskFilter>("all");

  if (blockedReason) {
    return (
      <EmptyState
        icon={Inbox}
        heading="Fix the filters to see approvals"
        subtext={blockedReason}
      />
    );
  }
  if (isLoading) {
    return <LoadingState variant="table" rows={5} className="border-0" />;
  }
  const invalid = isError ? validationErrorMessage(error) : null;
  if (invalid) {
    return (
      <ErrorState
        heading="Couldn’t apply these filters"
        subtext={invalid}
        onRetry={onRetry}
      />
    );
  }
  if (isError) {
    return (
      <ErrorState
        heading="Couldn’t load approvals"
        subtext="Something went wrong fetching your queue."
        onRetry={onRetry}
      />
    );
  }

  const autoItems = items.filter((i) => i.is_auto_approved);
  const queueItems = items.filter((i) => !i.is_auto_approved);

  // C7-25: ONE display predicate for BOTH lanes — the Auto-Approved lane must
  // honour the same search + risk refinement as the active queue.
  const q = search.trim().toLowerCase();
  const isFiltering = q !== "" || riskFilter !== "all";
  const matches = (i: ApprovalListItem): boolean => {
    if (riskFilter !== "all" && i.risk_level !== riskFilter) return false;
    if (
      q &&
      !`${i.subject_title ?? ""} ${i.submitter_name ?? ""}`
        .toLowerCase()
        .includes(q)
    ) {
      return false;
    }
    return true;
  };
  const filtered = queueItems.filter(matches);
  const filteredAuto = autoItems.filter(matches);

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-col gap-2 border-b border-border p-[var(--space-4)]">
        <div className="relative">
          <Search
            className="pointer-events-none absolute left-2 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search submitter or subject"
            aria-label="Search approvals"
            className="w-full rounded-atlas-md border border-border bg-background py-1.5 pl-8 pr-3 atlas-text-body focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-primary-600)]"
          />
        </div>
        <div className="flex gap-1" role="group" aria-label="Filter by risk">
          {RISK_FILTERS.map((r) => (
            <button
              key={r}
              type="button"
              onClick={() => setRiskFilter(r)}
              aria-pressed={riskFilter === r}
              className={cn(
                "rounded-atlas-full px-2.5 py-1 atlas-text-caption capitalize transition-colors",
                riskFilter === r
                  ? "bg-[var(--color-primary-600)] text-white"
                  : "bg-muted text-muted-foreground hover:bg-[var(--color-primary-50)]",
              )}
            >
              {r}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        {filtered.length === 0 ? (
          <EmptyState
            icon={Inbox}
            // C7-25: lane-appropriate copy. The active queue also holds
            // decided (approved/rejected) items, so "No pending approvals" was
            // wrong both when filtering and when only the auto lane has items.
            heading={
              queueItems.length === 0 && !serverFiltered
                ? "Approval queue is empty"
                : "No matching approvals"
            }
            subtext={
              queueItems.length === 0 && serverFiltered
                ? "No approvals match the selected filters. Clear filters to see the full queue."
                : queueItems.length === 0
                  ? "There are no items awaiting review in this queue."
                  : "No items in this queue match your search or filter."
            }
          />
        ) : (
          <ul className="flex flex-col">
            {filtered.map((it) => {
              const unread =
                !viewedIds.has(it.id) && it.current_status === "pending";
              return (
                <li key={it.id}>
                  <button
                    type="button"
                    onClick={() => onSelect(it.id)}
                    aria-current={selectedId === it.id}
                    data-approval-id={it.id}
                    className={cn(
                      "flex w-full flex-col gap-1.5 border-l-4 px-[var(--space-4)] py-[var(--space-3)] text-left hover:bg-muted",
                      selectedId === it.id
                        ? "border-[var(--color-primary-600)] bg-[var(--color-primary-50)]"
                        : "border-transparent",
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-2">
                        {unread && (
                          <span
                            className="h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--color-primary-600)]"
                            aria-label="Unread"
                          />
                        )}
                        <span className="truncate atlas-text-body font-medium text-foreground">
                          {it.subject_title ?? "Untitled"}
                        </span>
                      </span>
                      <StatusBadge variant={riskBadgeVariant(it.risk_level)} />
                    </div>
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex min-w-0 items-center gap-2">
                        <span className="truncate atlas-text-caption text-muted-foreground">
                          {it.submitter_name ?? "—"} ·{" "}
                          {formatMoney(it.subject_amount, it.currency)}
                        </span>
                        {requiredGradeLabel(it.required_approver_grade) && (
                          <span
                            data-testid="queue-grade-badge"
                            className="shrink-0 rounded-full border border-[var(--color-warning)] px-1.5 atlas-text-caption font-medium text-[var(--color-warning)]"
                          >
                            {requiredGradeLabel(it.required_approver_grade)}
                          </span>
                        )}
                      </span>
                      <StatusBadge
                        variant={statusBadgeVariant(
                          it.display_status || it.current_status,
                        )}
                        label={statusLabel(
                          it.display_status || it.current_status,
                        )}
                      />
                    </div>
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        <AutoApprovedLane
          items={filteredAuto}
          totalCount={autoItems.length}
          isFiltering={isFiltering}
          selectedId={selectedId}
          onSelect={onSelect}
        />
      </div>
    </div>
  );
}
