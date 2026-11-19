import * as React from "react";
import { LinkIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from "@/components/ui/tooltip";
import { citationLabel } from "@/lib/assistant/document-prefix";

/**
 * Atlas Citation Chip — clickable policy clause reference.
 *
 * Spec: UI_UX.md → Trust components → Citation chip
 * Background #F1F5F9, 1px #E2E8F0, hover #E2E8F0. Underline on hover.
 *
 * Two call shapes, same visual tokens:
 *   - Legacy: pass `clauseRef` (pre-formatted, e.g. "Travel Policy §4.1").
 *   - R6-T2 document-qualified: pass `documentType` + `ref` (+ optional
 *     `detail` snippet) and the chip renders `"<SHORT_PREFIX> §<ref>"` with a
 *     keyboard-accessible hover/focus preview of the cited snippet.
 */

export interface CitationChipProps {
  /** Pre-formatted clause reference (legacy planner path). */
  clauseRef?: string;
  /** Bare clause ref (e.g. "AIR-01") — combined with documentType into a label.
   *  Named `clauseId` (NOT `ref`): `ref` is a reserved React prop on function
   *  components and would be intercepted rather than passed through. */
  clauseId?: string;
  /** Backend document class — drives the short prefix ("T&E Policy", etc.). */
  documentType?: string | null;
  /** The cited snippet leaf text — shown as a hover/focus preview. */
  detail?: string;
  /** Click handler. */
  onClick?: () => void;
  className?: string;
}

const CHIP_CLASSES = [
  "inline-flex items-center gap-1.5",
  "px-3 py-1.5",
  "text-[length:var(--text-caption-size)] font-medium leading-none",
  "rounded-atlas-sm",
  "bg-[#F1F5F9] border border-[#E2E8F0]",
  "text-[#475569]",
  "transition-colors duration-atlas-fast",
  "hover:bg-[#E2E8F0] hover:underline",
  "focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)]",
];

export function CitationChip({
  clauseRef,
  clauseId,
  documentType,
  detail,
  onClick,
  className,
}: CitationChipProps) {
  // Legacy path renders the pre-formatted ref verbatim; the R6-T2 path builds a
  // document-qualified label from documentType + bare ref.
  const label =
    clauseRef !== undefined
      ? clauseRef
      : citationLabel(clauseId ?? "", documentType);

  const button = (
    <button
      type="button"
      onClick={onClick}
      className={cn(...CHIP_CLASSES, className)}
      aria-label={`Citation: ${label}`}
    >
      <LinkIcon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {label}
    </button>
  );

  // No snippet → plain chip (legacy behavior, unchanged visuals).
  if (!detail) return button;

  // Snippet preview: shadcn/Radix Tooltip opens on hover AND focus (keyboard
  // accessible). On touch, Radix opens the tooltip on tap/long-press; the
  // snippet is also exposed via aria-description-style title for assistive tech.
  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>{button}</TooltipTrigger>
        <TooltipContent
          role="tooltip"
          className="max-w-[280px] text-[#475569] whitespace-normal"
        >
          {detail}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
