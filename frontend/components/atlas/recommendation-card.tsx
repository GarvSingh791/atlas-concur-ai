import * as React from "react";
import { Sparkles } from "lucide-react";
import { StatusBadge } from "./status-badge";
import { CitationChip } from "./citation-chip";
import { AtlasButton } from "./button";
import { cn } from "@/lib/utils";

/**
 * Atlas Recommendation Card (AI) — used in Travel Planner.
 *
 * Spec: UI_UX.md → Cards → Recommendation card (AI)
 * Purple gradient header, title, rationale, source + policy-clause citation,
 * in-policy badge, Select action.
 */

export interface RecommendationCardProps {
  /** Option title (e.g. "Delta DL 487 · JFK → SFO"). */
  title: string;
  /** One-line rationale (e.g. "Best value within policy"). */
  rationale: string;
  /** Source reference (e.g. "Atlas Inventory"). */
  source: string;
  /** Policy clause reference (e.g. "§3.2 cabin class"). */
  policyClause?: string;
  /** Whether the option is in-policy. */
  inPolicy?: boolean;
  /** Select handler. */
  onSelect?: () => void;
  /** Whether this card is currently selected. */
  selected?: boolean;
  className?: string;
}

export function RecommendationCard({
  title,
  rationale,
  source,
  policyClause,
  inPolicy = true,
  onSelect,
  selected = false,
  className,
}: RecommendationCardProps) {
  return (
    <div
      className={cn(
        "rounded-atlas-md border border-[var(--card-border-color)] bg-[var(--surface-card)]",
        "shadow-atlas-1 overflow-hidden",
        "transition-all duration-atlas-fast",
        "hover:shadow-atlas-2 hover:-translate-y-[1px]",
        selected && "ring-2 ring-[var(--color-ai-primary)]",
        className,
      )}
    >
      {/* Purple gradient header */}
      <div
        className="flex items-center gap-2 px-[var(--space-4)] py-[var(--space-3)]"
        style={{
          background: `linear-gradient(135deg, var(--color-ai-gradient-start), var(--color-ai-gradient-end))`,
        }}
      >
        <Sparkles className="h-4 w-4 text-white shrink-0" aria-hidden="true" />
        <span className="text-[length:var(--text-caption-size)] font-semibold text-white">
          AI Recommendation
        </span>
      </div>

      {/* Body */}
      <div className="p-[var(--space-4)] space-y-3">
        {/* Title */}
        <h3 className="atlas-text-subtitle text-[var(--color-primary-900)]">
          {title}
        </h3>

        {/* Rationale */}
        <p className="atlas-text-body text-[#64748B]">{rationale}</p>

        {/* Source + citation */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[length:var(--text-caption-size)] text-[#64748B]">
            📎 Source: {source}
          </span>
          {policyClause && <CitationChip clauseRef={policyClause} />}
        </div>

        {/* Policy badge + action */}
        <div className="flex items-center justify-between pt-1">
          <StatusBadge variant={inPolicy ? "in-policy" : "out-of-policy"} />
          <AtlasButton variant="primary" size="sm" onClick={onSelect}>
            Select
          </AtlasButton>
        </div>
      </div>
    </div>
  );
}
