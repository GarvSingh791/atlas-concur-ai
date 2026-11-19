"use client";

import * as React from "react";

import { TripApprovalPanel } from "@/components/trips/trip-approval-panel";
import { TripSegmentList } from "@/components/trips/trip-segment-list";
import { useTrip } from "@/lib/query/trip-hooks";
import { tripApprovalView } from "@/lib/trips/approval";
import { tripTitle } from "@/lib/trips/summary";
import { isApiError } from "@/lib/types/api";
import { usePlannerStore } from "@/stores/planner-store";

/**
 * The planning conversation's trip: approval state + submit / resubmit (C7R),
 * and — UAT-2 Issue 4 — every leg saved on it with the estimated total.
 * Renders nothing until a trip has been saved from a selection.
 */
export function PlannerTripStatus({
  className,
  selecting = false,
}: {
  className?: string;
  /** A selection is being saved — Submit waits for it (no half trip submitted). */
  selecting?: boolean;
}) {
  const selectedTripId = usePlannerStore((s) => s.selectedTripId);
  const setSelectedTripId = usePlannerStore((s) => s.setSelectedTripId);
  const { data: trip, error } = useTrip(selectedTripId);

  // The trip is gone (deleted / not visible): forget it rather than break.
  React.useEffect(() => {
    if (selectedTripId && isApiError(error) && error.status === 404) {
      setSelectedTripId(null);
    }
  }, [error, selectedTripId, setSelectedTripId]);

  if (!trip) return null;
  // A trip returned for changes is still `draft`, but the planner never edits it
  // (owner decision 3) — it is changed and resubmitted from Trips.
  const returned = tripApprovalView(trip).stage === "returned";
  return (
    <TripApprovalPanel
      trip={trip}
      title={tripTitle(trip)}
      submitDisabled={selecting}
      className={className}
    >
      <TripSegmentList tripId={trip.id} estimatedCost={trip.estimated_cost} />
      {returned ? (
        <p className="atlas-text-caption text-muted-foreground">
          This trip was returned for changes, so the planner won&apos;t change
          it — review the approver&apos;s comment and resubmit it from Trips.
          Selecting another option here starts a new draft trip.
        </p>
      ) : (
        trip.status !== "draft" && (
          <p className="atlas-text-caption text-muted-foreground">
            This trip can no longer be changed. Selecting another option starts
            a new draft trip.
          </p>
        )
      )}
    </TripApprovalPanel>
  );
}
