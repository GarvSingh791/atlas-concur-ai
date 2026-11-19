import * as React from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";

import { CitationChip } from "@/components/atlas";
import { cn } from "@/lib/utils";
import type { ExceptionView } from "@/lib/approvals/risk-summary";

/**
 * Exceptions — one row per flagged item, with its reason and (for expense
 * subjects) a policy citation chip. All values come straight from risk_summary;
 * nothing is derived in the browser.
 */
export function ExceptionsList({
  exceptions,
  inPolicyNote,
}: {
  exceptions: ExceptionView[];
  inPolicyNote: string | null;
}) {
  if (exceptions.length === 0) {
    return (
      <p className="inline-flex items-center gap-2 atlas-text-body text-muted-foreground">
        <CheckCircle2
          className="h-4 w-4 shrink-0 text-[var(--color-success)]"
          aria-hidden="true"
        />
        No flagged exceptions.{inPolicyNote ? ` ${inPolicyNote}.` : ""}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-[var(--space-3)]">
      {exceptions.map((e) => (
        <div
          key={e.ref}
          className="rounded-atlas-md border border-border p-[var(--space-3)]"
        >
          <div className="flex items-start justify-between gap-2">
            <span className="inline-flex items-center gap-1.5 atlas-text-body font-medium text-foreground">
              <AlertTriangle
                className={cn(
                  "h-4 w-4 shrink-0",
                  e.severity === "blocking"
                    ? "text-[var(--color-error)]"
                    : "text-[var(--color-warning)]",
                )}
                aria-hidden="true"
              />
              {e.label}
              {e.typeLabel && (
                <span
                  className="atlas-text-caption font-normal text-muted-foreground"
                  data-testid="exception-type"
                >
                  · {e.typeLabel}
                </span>
              )}
            </span>
            {e.clauseRef && <CitationChip clauseRef={e.clauseRef} />}
          </div>
          {e.reason && (
            <p className="atlas-text-caption text-muted-foreground mt-1.5 break-words">
              {e.reason}
            </p>
          )}
          {e.justification && (
            <figure className="mt-2 min-w-0 rounded-atlas-sm border-l-2 border-border bg-muted/40 px-[var(--space-2)] py-1.5">
              <figcaption className="atlas-text-caption font-medium text-foreground">
                Claimant justification
              </figcaption>
              <blockquote className="atlas-text-caption text-muted-foreground mt-0.5 whitespace-pre-wrap break-words">
                “{e.justification}”
              </blockquote>
            </figure>
          )}
        </div>
      ))}
      {inPolicyNote && (
        <p className="atlas-text-caption text-muted-foreground">
          {inPolicyNote}
        </p>
      )}
    </div>
  );
}
