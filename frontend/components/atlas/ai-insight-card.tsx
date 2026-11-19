import * as React from "react";
import { Sparkles } from "lucide-react";
import { ConfidenceIndicator } from "./confidence-indicator";
import { CitationChip } from "./citation-chip";
import { AIReasoningPanel } from "./ai-reasoning-panel";
import { cn } from "@/lib/utils";

/**
 * Atlas AI Insight Card — dashboards / report detail.
 *
 * Spec: UI_UX.md → Cards → AI insight card
 * Purple header, body text, citation chip, confidence bar,
 * expandable "Why this?" reasoning.
 */

export interface AIInsightCardProps {
  /** Header title. Default: "AI Analysis". */
  title?: string;
  /** Analysis body text. */
  body: string;
  /** Policy clause citation (e.g. "Travel Policy §4.1"). */
  citation?: string;
  /** Confidence value 0–100. */
  confidence?: number;
  /** Reasoning steps for the collapsible panel. */
  reasoning?: string[];
  /** Start reasoning panel expanded. */
  defaultExpanded?: boolean;
  className?: string;
}

export function AIInsightCard({
  title = "AI Analysis",
  body,
  citation,
  confidence,
  reasoning,
  defaultExpanded = false,
  className,
}: AIInsightCardProps) {
  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
        "shadow-atlas-1 overflow-hidden",
        className,
      )}
    >
      {/* Purple header */}
      <div
        className="flex items-center gap-2 px-[var(--space-4)] py-[var(--space-3)]"
        style={{
          background: `linear-gradient(135deg, var(--color-ai-gradient-start), var(--color-ai-gradient-end))`,
        }}
      >
        <Sparkles className="h-4 w-4 text-white shrink-0" aria-hidden="true" />
        <span className="text-[length:var(--text-caption-size)] font-semibold text-white">
          {title}
        </span>
      </div>

      {/* Body */}
      <div className="p-[var(--space-4)] space-y-3">
        {/* Analysis text */}
        <p className="atlas-text-body text-[#334155]">{body}</p>

        {/* Citation + confidence row */}
        <div className="flex flex-wrap items-center gap-3">
          {citation && <CitationChip clauseRef={citation} />}
          {confidence !== undefined && (
            <ConfidenceIndicator value={confidence} />
          )}
        </div>

        {/* Reasoning panel */}
        {reasoning && reasoning.length > 0 && (
          <AIReasoningPanel
            title="Why this assessment?"
            steps={reasoning}
            defaultExpanded={defaultExpanded}
          />
        )}
      </div>
    </div>
  );
}
