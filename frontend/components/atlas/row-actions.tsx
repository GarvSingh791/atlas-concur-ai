"use client";

import * as React from "react";
import { MoreHorizontal } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Row actions — follow-up (UI polish).
 *
 * The per-row action buttons of the entries table, the trip cards and the
 * users table used to wrap onto two lines on narrow widths. RowActions keeps
 * them on ONE line (flex-nowrap) and, below the `sm` breakpoint, folds them
 * behind a single "More actions" toggle that expands them in place.
 *
 * The actions stay MOUNTED while folded (only `hidden`), so state and dialogs
 * the parent owns are never torn down by opening / closing the menu. The
 * expanded list is in normal flow (not a floating popover), so it is never
 * clipped by a table's horizontal-scroll container. Without matchMedia (SSR,
 * tests) the actions render inline.
 */

/** Below Tailwind's `sm` (640px) the actions fold behind one toggle. */
export const ROW_ACTIONS_NARROW_QUERY = "(max-width: 639px)";

function useNarrow(query: string): boolean {
  const [narrow, setNarrow] = React.useState(false);
  React.useEffect(() => {
    if (
      typeof window === "undefined" ||
      typeof window.matchMedia !== "function"
    )
      return;
    const mql = window.matchMedia(query);
    const sync = () => setNarrow(mql.matches);
    sync();
    mql.addEventListener?.("change", sync);
    return () => mql.removeEventListener?.("change", sync);
  }, [query]);
  return narrow;
}

export interface RowActionsProps {
  children: React.ReactNode;
  /** Accessible name of the fold toggle, e.g. "More actions for Hotel". */
  label?: string;
  className?: string;
}

export function RowActions({
  children,
  label = "More actions",
  className,
}: RowActionsProps) {
  const narrow = useNarrow(ROW_ACTIONS_NARROW_QUERY);
  const [open, setOpen] = React.useState(false);
  const listId = React.useId();

  React.useEffect(() => {
    if (!narrow) setOpen(false);
  }, [narrow]);

  if (!narrow) {
    return (
      <div
        data-testid="row-actions"
        className={cn(
          "flex shrink-0 flex-nowrap items-center justify-end gap-1 whitespace-nowrap",
          className,
        )}
      >
        {children}
      </div>
    );
  }

  return (
    <div
      data-testid="row-actions"
      data-folded="true"
      className={cn("flex shrink-0 flex-col items-end gap-1", className)}
      onKeyDown={(e) => {
        if (e.key === "Escape" && open) {
          e.stopPropagation();
          setOpen(false);
        }
      }}
    >
      <button
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-8 w-8 items-center justify-center rounded-atlas-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <MoreHorizontal className="h-4 w-4" aria-hidden="true" />
      </button>
      <div
        id={listId}
        role="group"
        aria-label={label}
        hidden={!open}
        className={cn(
          // `hidden` class too: Tailwind's `flex` would beat the [hidden] attribute.
          open ? "flex" : "hidden",
          "flex-col items-stretch gap-1 rounded-atlas-md border border-border bg-card p-1 shadow-sm",
        )}
      >
        {children}
      </div>
    </div>
  );
}
