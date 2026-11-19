import * as React from "react";
import { Search, Sparkles, Pencil } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * Atlas Provenance Badge — maps to receipts.field_provenance.
 *
 * Spec: UI_UX.md → Trust components → Provenance badge
 * 12px text, 6px 10px padding, rounded-full.
 */

export type ProvenanceType = "deterministic" | "ai_inferred" | "user_edited";

const PROVENANCE_CONFIG: Record<
  ProvenanceType,
  {
    label: string;
    icon: React.ElementType;
    bg: string;
    text: string;
  }
> = {
  deterministic: {
    label: "OCR Extracted",
    icon: Search,
    bg: "bg-[#DBEAFE]",
    text: "text-[var(--color-info)]",
  },
  ai_inferred: {
    label: "AI Inferred",
    icon: Sparkles,
    bg: "bg-[#EDE9FE]",
    text: "text-[var(--color-ai-primary)]",
  },
  user_edited: {
    label: "User Edited",
    icon: Pencil,
    bg: "bg-[#DCFCE7]",
    text: "text-[#16A34A]",
  },
};

export interface ProvenanceBadgeProps {
  type: ProvenanceType;
  className?: string;
}

export function ProvenanceBadge({ type, className }: ProvenanceBadgeProps) {
  const config = PROVENANCE_CONFIG[type];
  const Icon = config.icon;

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1",
        "px-[10px] py-[6px]",
        "text-[12px] font-medium leading-none",
        "rounded-atlas-full",
        config.bg,
        config.text,
        className,
      )}
    >
      <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
      {config.label}
    </span>
  );
}
