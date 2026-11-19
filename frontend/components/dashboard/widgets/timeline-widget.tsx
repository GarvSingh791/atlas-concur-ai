"use client";

import {
  LoadingState,
  EmptyState,
  ErrorState,
  StatusBadge,
  humanizeStatus,
} from "@/components/atlas";
import type { StatusBadgeVariant } from "@/components/atlas";

interface TripListItem {
  id: string;
  origin: string;
  primary_destination: string;
  start_date: string;
  end_date: string;
  status: string;
  currency: string;
}

interface Page<T> {
  items: T[];
  total: number;
}

interface TimelineWidgetProps {
  data: Page<TripListItem> | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
}

function tripStatusVariant(status: string): StatusBadgeVariant {
  switch (status) {
    case "approved":
    case "completed": // trip is done = success-adjacent colour, its own label
      return "approved";
    case "pending_approval":
    case "pending":
      return "pending";
    case "rejected":
      return "rejected";
    default:
      // Unknown trip status → neutral, never a misleading "Pending".
      return "neutral";
  }
}

// QA round: every trip status is labelled for what it is — a completed trip
// read "Approved" here while /trips said "Completed".
const TRIP_STATUS_LABEL: Record<string, string> = {
  draft: "Draft",
  pending_approval: "Pending approval",
  pending: "Pending approval",
  approved: "Approved",
  rejected: "Rejected",
  cancelled: "Cancelled",
  completed: "Completed",
};

export function tripStatusLabel(status: string): string {
  return TRIP_STATUS_LABEL[status] ?? humanizeStatus(status);
}

export function TimelineWidget({
  data,
  isLoading,
  isError,
  onRetry,
}: TimelineWidgetProps) {
  if (isLoading) return <LoadingState variant="table" rows={4} />;

  if (isError) {
    return (
      <ErrorState
        heading="Failed to load trips"
        subtext="Could not fetch trip timeline."
        onRetry={onRetry}
      />
    );
  }

  const trips = data?.items ?? [];

  if (trips.length === 0) {
    return (
      <EmptyState
        heading="No trips found"
        subtext="Your upcoming trips will appear here."
      />
    );
  }

  return (
    <div className="divide-y divide-border rounded-atlas-md border border-border bg-card">
      {trips.map((trip) => (
        <div
          key={trip.id}
          className="flex items-center justify-between gap-3 px-4 py-3"
        >
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm font-medium">
              {trip.origin} → {trip.primary_destination}
            </p>
            <p className="truncate text-xs text-muted-foreground">
              {trip.start_date} – {trip.end_date}
            </p>
          </div>
          <span
            className="shrink-0 whitespace-nowrap"
            data-testid={`timeline-status-${trip.id}`}
          >
            <StatusBadge
              variant={tripStatusVariant(trip.status)}
              label={tripStatusLabel(trip.status)}
            />
          </span>
        </div>
      ))}
    </div>
  );
}
