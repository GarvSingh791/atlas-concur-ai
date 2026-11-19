"use client";

import * as React from "react";
import { Search, ChevronDown, Plus, Filter } from "lucide-react";

import { AtlasButton } from "@/components/atlas";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import type { ExpenseReportStatus } from "@/lib/types";
import { REPORT_STATUSES, REPORT_STATUS_LABELS } from "@/lib/expenses/format";

/** "all" plus every known lifecycle status, plus withdrawn (product gap 4). */
export type StatusFilter = "all" | ExpenseReportStatus | "withdrawn";

const FILTER_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All statuses" },
  // Derived from the single label map so a new lifecycle state is filterable
  // as soon as it is added there.
  ...REPORT_STATUSES.map((value) => ({
    value,
    label: REPORT_STATUS_LABELS[value],
  })),
  // Withdrawn reports are hidden from "All statuses" (server default) and
  // listed only through this filter.
  { value: "withdrawn", label: "Withdrawn" },
];

export interface ReportListToolbarProps {
  statusFilter: StatusFilter;
  onStatusFilterChange: (value: StatusFilter) => void;
  search: string;
  onSearchChange: (value: string) => void;
  /** C7R UAT: open the "New report" dialog. Omitted → no create action. */
  onNewReport?: () => void;
}

export function ReportListToolbar({
  statusFilter,
  onStatusFilterChange,
  search,
  onSearchChange,
  onNewReport,
}: ReportListToolbarProps) {
  const activeLabel =
    FILTER_OPTIONS.find((o) => o.value === statusFilter)?.label ??
    "All statuses";

  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {/* Status filter */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <AtlasButton variant="secondary" size="sm">
              <Filter className="h-4 w-4" aria-hidden="true" />
              {activeLabel}
              <ChevronDown className="h-4 w-4" aria-hidden="true" />
            </AtlasButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            {FILTER_OPTIONS.map((opt) => (
              <DropdownMenuItem
                key={opt.value}
                onSelect={() => onStatusFilterChange(opt.value)}
                className={cn(
                  statusFilter === opt.value && "font-semibold text-foreground",
                )}
              >
                {opt.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        {/* Search (client-side title filter — the list endpoint has no search param) */}
        <div className="relative min-w-0 flex-1 sm:max-w-xs">
          <Search
            className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="Search reports"
            aria-label="Search reports by name"
            className="pl-9"
          />
        </div>
      </div>

      {/* C7R UAT: a working create action replaces the disabled "Assemble
          report" button and its dev note. New reports start empty; entries are
          moved in from another draft (e.g. the Receipt Inbox) in the detail. */}
      {onNewReport && (
        <AtlasButton variant="primary" size="sm" onClick={onNewReport}>
          <Plus className="h-4 w-4" aria-hidden="true" />
          New report
        </AtlasButton>
      )}
    </div>
  );
}
