"use client";

import * as React from "react";
import { MapPin } from "lucide-react";
import { usePlannerStore } from "@/stores/planner-store";
import { EmptyState } from "@/components/atlas/empty-state";
import { ErrorState } from "@/components/atlas/error-state";
import { OptionCard } from "./option-card";
import { useSelectedOptionIds } from "@/lib/planner/use-trip-selection";
import { cn } from "@/lib/utils";

interface RecommendationPanelProps {
  onSelectOption: (optionId: string) => void;
  /** The option being saved right now (selection in flight), else null. */
  selectingId?: string | null;
  error?: string | null;
  onRetry?: () => void;
  className?: string;
}

function SkeletonCard() {
  return (
    <div className="bg-white rounded-atlas-md border border-[#E2E8F0] p-[var(--space-4)] space-y-3 animate-pulse">
      <div className="flex justify-between">
        <div className="h-5 bg-[#E2E8F0] rounded w-1/2" />
        <div className="h-6 bg-[#E2E8F0] rounded w-16" />
      </div>
      <div className="h-4 bg-[#F1F5F9] rounded w-3/4" />
      <div className="h-4 bg-[#F1F5F9] rounded w-1/2" />
      <div className="flex justify-between items-center mt-2">
        <div className="h-4 bg-[#F1F5F9] rounded w-24" />
        <div className="h-8 bg-[#E0E7FF] rounded w-20" />
      </div>
    </div>
  );
}

export function RecommendationPanel({
  onSelectOption,
  selectingId,
  error,
  onRetry,
  className,
}: RecommendationPanelProps) {
  const shortlist = usePlannerStore((s) => s.shortlist);
  // UAT-2 Issue 4: the flight AND the hotel on the conversation's trip are
  // "Selected" (from its saved segments), plus the option being saved now.
  const selectedIds = useSelectedOptionIds(selectingId ?? null);
  const streaming = usePlannerStore((s) => s.streaming);
  const noOptionsMessage = usePlannerStore((s) => s.noOptionsMessage);

  if (error) {
    return (
      <div
        className={cn(
          "flex items-center justify-center h-full p-[var(--space-6)]",
          className,
        )}
      >
        <ErrorState
          heading="Could not load recommendations"
          subtext={error}
          onRetry={onRetry}
        />
      </div>
    );
  }

  if (streaming && shortlist.length === 0) {
    return (
      <div
        className={cn("p-[var(--space-4)] space-y-[var(--space-3)]", className)}
      >
        <SkeletonCard />
        <SkeletonCard />
        <SkeletonCard />
      </div>
    );
  }

  if (!streaming && shortlist.length === 0) {
    // A searched-but-empty plan shows the server's reason (what was searched
    // and what the inventory covers) instead of the generic prompt.
    return (
      <div className={cn("flex items-center justify-center h-full", className)}>
        {noOptionsMessage ? (
          <EmptyState
            icon={MapPin}
            heading="No options found"
            subtext={noOptionsMessage}
          />
        ) : (
          <EmptyState
            icon={MapPin}
            heading="No options yet"
            subtext="Describe your trip in the conversation panel to get recommendations."
          />
        )}
      </div>
    );
  }

  return (
    <div
      className={cn(
        // pb-20: the last card's Select can scroll clear of the fixed AI FAB
        // (56px, 24px above the viewport bottom) at every width.
        "overflow-y-auto p-[var(--space-4)] pb-20 space-y-[var(--space-3)]",
        className,
      )}
    >
      {shortlist.map((option) => (
        <OptionCard
          key={option.option_id}
          option={option}
          isSelected={selectedIds.has(option.option_id)}
          onSelect={onSelectOption}
          disabled={!!selectingId}
        />
      ))}
    </div>
  );
}
