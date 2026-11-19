"use client";

import * as React from "react";
import {
  ChevronRight,
  ChevronDown,
  Check,
  X,
  Loader2,
  Wrench,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  type ToolStep,
  summarizeToolActivity,
} from "@/lib/assistant/tool-activity";

/**
 * Tool-activity affordance (P5-T10) — reflects the REAL tool layer from the
 * persisted grounding trace. Active (spinner+blue) / Complete (green check) /
 * Failed (red ✕). Auto-collapses to a summary once the response is in.
 *
 * Renders nothing when there is no trace and the turn is not streaming — it
 * never invents tool steps.
 */

export interface ToolActivityIndicatorProps {
  steps: ToolStep[];
  /** The turn is still streaming — show a generic working state. */
  streaming?: boolean;
  className?: string;
}

function StatusIcon({ status }: { status: ToolStep["status"] }) {
  if (status === "complete") {
    return (
      <Check
        className="h-3.5 w-3.5 shrink-0 text-[var(--color-success)]"
        aria-hidden="true"
      />
    );
  }
  if (status === "failed") {
    return (
      <X
        className="h-3.5 w-3.5 shrink-0 text-[var(--color-error)]"
        aria-hidden="true"
      />
    );
  }
  return (
    <Loader2
      className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-info)]"
      aria-hidden="true"
    />
  );
}

export function ToolActivityIndicator({
  steps,
  streaming,
  className,
}: ToolActivityIndicatorProps) {
  // Expanded while streaming; auto-collapsed to a summary afterwards.
  const [expanded, setExpanded] = React.useState(false);
  const contentId = React.useId();

  React.useEffect(() => {
    setExpanded(Boolean(streaming));
  }, [streaming]);

  const hasSteps = steps.length > 0;
  if (!hasSteps && !streaming) return null;

  const summary = streaming
    ? hasSteps
      ? "Working…"
      : "Working…"
    : summarizeToolActivity(steps);

  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[#E2E8F0] bg-[#F8FAFC] px-3 py-2",
        className,
      )}
    >
      <button
        type="button"
        onClick={() => hasSteps && setExpanded((e) => !e)}
        className={cn(
          "flex w-full items-center gap-2 text-left text-[length:var(--text-caption-size)] text-[#475569]",
          hasSteps ? "cursor-pointer" : "cursor-default",
          "focus-visible:atlas-focus-ring rounded-atlas-sm",
        )}
        aria-expanded={hasSteps ? expanded : undefined}
        aria-controls={hasSteps ? contentId : undefined}
        disabled={!hasSteps}
      >
        {streaming ? (
          <Loader2
            className="h-3.5 w-3.5 shrink-0 animate-spin text-[var(--color-info)]"
            aria-hidden="true"
          />
        ) : (
          <Wrench
            className="h-3.5 w-3.5 shrink-0 text-[#64748B]"
            aria-hidden="true"
          />
        )}
        <span className="flex-1 font-medium">{summary}</span>
        {hasSteps &&
          (expanded ? (
            <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          ))}
      </button>

      {hasSteps && expanded && (
        <ul id={contentId} className="mt-2 space-y-1.5">
          {steps.map((step) => (
            <li
              key={step.id}
              className="flex items-center gap-2 text-[length:var(--text-caption-size)] text-[#334155]"
            >
              <StatusIcon status={step.status} />
              <span className="font-medium">{step.name}</span>
              {step.detail && (
                <span className="text-[#94A3B8]">· {step.detail}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
