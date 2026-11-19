"use client";

import * as React from "react";

import { useTripSegments } from "@/lib/query/trip-hooks";
import { formatUsd, segmentLine } from "@/lib/trips/summary";
import { cn } from "@/lib/utils";

/**
 * The legs saved on a trip, one line each, plus the estimated total
 * (UAT-2 Issue 4: a planner trip holds its flight AND its hotel).
 */
export function TripSegmentList({
  tripId,
  estimatedCost,
  className,
}: {
  tripId: string;
  estimatedCost?: string | null;
  className?: string;
}) {
  const { data: segments, isLoading } = useTripSegments(tripId);
  const total = formatUsd(estimatedCost);

  if (isLoading) {
    return (
      <p className={cn("atlas-text-caption text-muted-foreground", className)}>
        Loading trip details…
      </p>
    );
  }
  const rows = segments ?? [];
  return (
    <div className={cn("flex flex-col gap-1", className)}>
      {rows.length === 0 ? (
        <p className="atlas-text-caption text-muted-foreground">
          No flights or hotels saved on this trip yet.
        </p>
      ) : (
        <ul className="flex flex-col gap-0.5" aria-label="Trip segments">
          {rows.map((seg) => (
            <li key={seg.id} className="atlas-text-body text-foreground">
              {segmentLine(seg)}
            </li>
          ))}
        </ul>
      )}
      {total && (
        <p className="atlas-text-body font-medium text-foreground">
          Estimated total {total}
        </p>
      )}
    </div>
  );
}
