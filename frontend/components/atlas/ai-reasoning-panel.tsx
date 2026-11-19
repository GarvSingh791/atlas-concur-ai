"use client";

import * as React from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Atlas AI Reasoning Panel — collapsible "Why this recommendation?" affordance.
 *
 * Spec: UI_UX.md → Trust components → AI reasoning panel
 * Collapsed: "▶ Why this recommendation?" (14px, primary color)
 * Expanded: "▼" + numbered step list
 * Background var(--color-ai-surface), radius 12px, padding 16px.
 */

export interface AIReasoningPanelProps {
  /** Panel title. Default: "Why this recommendation?" */
  title?: string;
  /** Numbered reasoning steps. */
  steps: string[];
  /** Start expanded. Default: false. */
  defaultExpanded?: boolean;
  className?: string;
}

export function AIReasoningPanel({
  title = "Why this recommendation?",
  steps,
  defaultExpanded = false,
  className,
}: AIReasoningPanelProps) {
  const [expanded, setExpanded] = React.useState(defaultExpanded);
  const contentId = React.useId();

  return (
    <div
      className={cn(
        "rounded-atlas-lg bg-[var(--color-ai-surface)] p-[var(--space-4)]",
        className,
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "flex items-center gap-1.5 w-full text-left",
          "text-[length:var(--text-body-size)] font-medium",
          "text-[var(--color-primary-600)]",
          "transition-colors duration-atlas-fast",
          "hover:text-[var(--color-primary-700)]",
          "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)]",
          "rounded-atlas-sm",
        )}
        aria-expanded={expanded}
        aria-controls={contentId}
      >
        {expanded ? (
          <ChevronDown className="h-4 w-4 shrink-0" aria-hidden="true" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0" aria-hidden="true" />
        )}
        {title}
      </button>

      <div
        id={contentId}
        role="region"
        aria-label={title}
        className={cn(
          "overflow-hidden transition-all duration-atlas-normal",
          expanded ? "mt-3 max-h-[500px] opacity-100" : "max-h-0 opacity-0",
        )}
      >
        <ol className="list-none space-y-2 pl-6">
          {steps.map((step, index) => (
            <li
              key={index}
              className="flex gap-2 text-[length:var(--text-body-size)] text-[#475569] leading-[var(--text-body-line-height)]"
            >
              <span className="shrink-0 font-semibold text-[var(--color-ai-primary)]">
                {index + 1}.
              </span>
              {step}
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}
