"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { Briefcase, ChevronDown, ChevronRight } from "lucide-react";

import { EmptyState, ErrorState, LoadingState } from "@/components/atlas";
import { TripApprovalPanel } from "@/components/trips/trip-approval-panel";
import { TripSegmentList } from "@/components/trips/trip-segment-list";
import { useTrips } from "@/lib/query/trip-hooks";
import { tripTitle } from "@/lib/trips/summary";
import type { TripListItem, TripStatus } from "@/lib/types";

/** Product gaps 4 / 8: every trip state is filterable, withdrawn included. */
export type TripStatusFilter = "all" | TripStatus | "withdrawn";

export const TRIP_FILTER_OPTIONS: { value: TripStatusFilter; label: string }[] =
  [
    { value: "all", label: "All statuses" },
    { value: "draft", label: "Draft" },
    { value: "pending_approval", label: "Awaiting approval" },
    { value: "approved", label: "Approved" },
    { value: "completed", label: "Completed" },
    { value: "rejected", label: "Rejected" },
    { value: "cancelled", label: "Cancelled" },
    { value: "withdrawn", label: "Withdrawn" },
  ];

/** Client-side filter over the fetched trips (server fields only). */
export function filterTrips(
  trips: TripListItem[],
  filter: TripStatusFilter,
): TripListItem[] {
  if (filter === "all") return trips;
  if (filter === "withdrawn")
    return trips.filter((t) => Boolean(t.withdrawn_at));
  if (filter === "cancelled")
    return trips.filter((t) => t.status === "cancelled" && !t.withdrawn_at);
  return trips.filter((t) => t.status === filter);
}

/**
 * My Trips — C7R. Lists the traveler's trips with their approval state, and
 * lets them submit a draft trip or resubmit one returned for changes.
 * UAT-2 Issue 4: each card is titled by route + dates + purpose and opens to
 * show its saved flights / hotels and estimated total.
 *
 * UAT2: `useTrips` sends `mine=true`, so a manager sees only their OWN trips
 * here (never their direct reports' — team review lives on /approvals).
 */
export function TripList() {
  const { data, isLoading, isError, refetch } = useTrips();
  const [filter, setFilter] = React.useState<TripStatusFilter>("all");
  // Product gap 3: "/trips?trip=<id>" (notifications, global search) expands and
  // scrolls to that trip.
  const linkedTripId = useSearchParams()?.get("trip") ?? null;

  if (isLoading) return <LoadingState rows={3} />;
  if (isError) {
    return (
      <ErrorState
        heading="Couldn’t load your trips"
        subtext="Something went wrong. Please try again."
        onRetry={() => refetch()}
      />
    );
  }
  const trips = data?.items ?? [];
  if (trips.length === 0) {
    return (
      <EmptyState
        icon={Briefcase}
        heading="No trips yet"
        subtext="Plan a trip in the planner; selecting an option saves it here as a draft."
      />
    );
  }
  const shown = filterTrips(trips, filter);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-2">
        <label
          htmlFor="trip-status-filter"
          className="atlas-text-caption font-medium text-muted-foreground"
        >
          Status
        </label>
        <select
          id="trip-status-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as TripStatusFilter)}
          className="h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground"
        >
          {TRIP_FILTER_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {shown.length === 0 ? (
        <EmptyState
          icon={Briefcase}
          heading="No matching trips"
          subtext="Try a different status filter."
        />
      ) : (
        <ul className="flex flex-col gap-3" aria-label="My trips">
          {shown.map((trip) => (
            <li key={trip.id}>
              <TripCard trip={trip} highlighted={trip.id === linkedTripId} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TripCard({
  trip,
  highlighted = false,
}: {
  trip: TripListItem;
  highlighted?: boolean;
}) {
  const [open, setOpen] = React.useState(highlighted);
  const anchorRef = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    if (!highlighted) return;
    setOpen(true);
    anchorRef.current?.scrollIntoView?.({ block: "center" });
  }, [highlighted]);
  const detailsId = `trip-details-${trip.id}`;
  return (
    <div
      ref={anchorRef}
      data-linked={highlighted || undefined}
      className={
        highlighted
          ? "rounded-atlas-lg ring-2 ring-[var(--focus-ring-color)]"
          : undefined
      }
    >
      <TripApprovalPanel trip={trip} title={tripTitle(trip)} allowDiscard>
        <button
          type="button"
          className="atlas-text-caption flex w-fit items-center gap-1 font-medium text-[var(--color-primary-600)] hover:underline"
          aria-expanded={open}
          aria-controls={detailsId}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? (
            <ChevronDown className="h-4 w-4" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          )}
          {open ? "Hide trip details" : "Show trip details"}
        </button>
        {open && (
          <div id={detailsId}>
            <TripSegmentList
              tripId={trip.id}
              estimatedCost={trip.estimated_cost ?? null}
            />
          </div>
        )}
      </TripApprovalPanel>
    </div>
  );
}
