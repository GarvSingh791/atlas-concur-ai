"use client";

import * as React from "react";
import { ChevronDown, ChevronRight, Sparkles } from "lucide-react";

import { StatusBadge } from "@/components/atlas";
import { formatMoney } from "@/lib/approvals/format";
import { cn } from "@/lib/utils";
import type { ApprovalListItem } from "@/lib/types/approvals";

/**
 * Auto-Approved lane (US-M2) — a collapsed section at the foot of the queue.
 * Each row shows its rationale (the system auto_approve step's reason) + a Low
 * risk badge. Inspect on click (reverse-within-policy happens in the panel).
 * GUARDRAIL: there is NO "auto-rejected" lane — the agent never auto-rejects.
 */
export function AutoApprovedLane({
  items,
  totalCount,
  isFiltering = false,
  selectedId,
  onSelect,
}: {
  /** The auto-approved items AFTER the queue's search + risk refinement. */
  items: ApprovalListItem[];
  /** Unfiltered auto-approved count (defaults to items.length). */
  totalCount?: number;
  /** True while a search / risk filter is active (C7-25). */
  isFiltering?: boolean;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = React.useState(false);
  const total = totalCount ?? items.length;

  if (total === 0) return null;

  const countLabel =
    isFiltering && items.length !== total
      ? `${items.length} of ${total}`
      : String(items.length);

  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <section className="border-t border-border">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-[var(--space-4)] py-[var(--space-3)] text-left atlas-text-caption font-semibold uppercase tracking-wide text-muted-foreground hover:bg-muted"
      >
        <Chevron className="h-4 w-4 shrink-0" aria-hidden="true" />
        <Sparkles
          className="h-4 w-4 shrink-0 text-atlas-ai-primary"
          aria-hidden="true"
        />
        Auto-Approved ({countLabel})
      </button>

      {open && items.length === 0 && (
        <p className="px-[var(--space-4)] pb-[var(--space-3)] atlas-text-caption text-muted-foreground">
          No auto-approved items match your search or filter.
        </p>
      )}

      {open && items.length > 0 && (
        <ul className="flex flex-col">
          {items.map((it) => (
            <li key={it.id}>
              <button
                type="button"
                onClick={() => onSelect(it.id)}
                aria-current={selectedId === it.id}
                data-approval-id={it.id}
                className={cn(
                  "flex w-full flex-col gap-1 border-l-4 px-[var(--space-4)] py-[var(--space-3)] text-left hover:bg-muted",
                  selectedId === it.id
                    ? "border-[var(--color-primary-600)] bg-[var(--color-primary-50)]"
                    : "border-transparent",
                )}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="atlas-text-body truncate text-foreground">
                    {it.subject_title ?? "Untitled"}
                  </span>
                  <StatusBadge variant="risk-low" />
                </div>
                <span className="atlas-text-caption text-muted-foreground">
                  {it.submitter_name ?? "—"} ·{" "}
                  {formatMoney(it.subject_amount, it.currency)}
                </span>
                {it.auto_approve_rationale && (
                  <span className="atlas-text-caption italic text-muted-foreground line-clamp-2">
                    {it.auto_approve_rationale}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
