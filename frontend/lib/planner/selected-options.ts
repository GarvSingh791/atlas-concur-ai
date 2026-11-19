/**
 * Which shortlist cards are "Selected" (UAT-2 Issue 4).
 *
 * The planner's trip holds one leg per type, so BOTH the flight and the hotel
 * show Selected. The truth is the saved trip's segments (it survives a reload),
 * not a single client-side id — and only while the trip matches the current
 * request: after a route / date change, picking an option starts a new trip, so
 * nothing on screen is "on" the current trip yet.
 */
import type { PlannerIntent, Trip, TripSegment } from "@/lib/types";

const SHOWN_STATUSES = new Set([
  "draft",
  "pending_approval",
  "approved",
  "completed",
]);

function norm(v: string | null | undefined): string {
  return (v ?? "").trim().toUpperCase();
}

export function tripMatchesIntent(
  trip: Pick<
    Trip,
    "origin" | "primary_destination" | "start_date" | "end_date"
  >,
  intent: Pick<
    PlannerIntent,
    "origin" | "destination" | "departure_date" | "return_date"
  >,
): boolean {
  if (!intent.departure_date) return false;
  return (
    norm(trip.origin) === norm(intent.origin) &&
    norm(trip.primary_destination) === norm(intent.destination) &&
    trip.start_date === intent.departure_date &&
    trip.end_date === (intent.return_date || intent.departure_date)
  );
}

export function savedOptionIds(
  trip: Trip | null | undefined,
  segments: TripSegment[] | null | undefined,
  intent: PlannerIntent,
): Set<string> {
  const ids = new Set<string>();
  if (!trip || !segments || !SHOWN_STATUSES.has(trip.status)) return ids;
  if (!tripMatchesIntent(trip, intent)) return ids;
  for (const seg of segments) {
    const id = seg.details?.option_id;
    if (typeof id === "string" && id) ids.add(id);
  }
  return ids;
}
