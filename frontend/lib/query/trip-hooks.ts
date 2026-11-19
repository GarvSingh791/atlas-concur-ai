"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { keepCallerItems } from "@/lib/query/own-items";
import type { Trip, TripListItem, TripSegment } from "@/lib/types";
import type { PaginatedResponse } from "@/lib/types/api";

export const tripQueryKeys = {
  // "mine" segment: the personal list (see useTrips).
  list: () => ["trips", "list", "mine"] as const,
  trip: (id: string) => ["trips", id] as const,
  tripSegments: (id: string) => ["trips", id, "segments"] as const,
};

/**
 * The caller's OWN trips ("My Trips", the new-report trip picker). Always sends
 * `mine=true` (UAT2): without it a manager would get their direct reports'
 * trips too — team review lives on /approvals.
 */
export function useTrips() {
  return useQuery<PaginatedResponse<TripListItem>>({
    queryKey: tripQueryKeys.list(),
    queryFn: async () =>
      keepCallerItems(
        await api.get<PaginatedResponse<TripListItem>>("/trips", {
          params: { size: 50, mine: true },
        }),
        (t) => t.traveler_id,
      ),
  });
}

export function fetchTrip(id: string): Promise<Trip> {
  return api.get<Trip>(`/trips/${id}`);
}

export function fetchTripSegments(tripId: string): Promise<TripSegment[]> {
  return api.get<TripSegment[]>(`/trips/${tripId}/segments`);
}

export function useTrip(id: string | null) {
  return useQuery<Trip>({
    queryKey: tripQueryKeys.trip(id ?? ""),
    queryFn: () => fetchTrip(id as string),
    enabled: !!id,
  });
}

export function useTripSegments(tripId: string | null) {
  return useQuery<TripSegment[]>({
    queryKey: tripQueryKeys.tripSegments(tripId ?? ""),
    queryFn: () => fetchTripSegments(tripId as string),
    enabled: !!tripId,
  });
}

/**
 * Discard a never-submitted draft trip — PATCH /trips/{id} {status: "cancelled"}
 * (draft -> cancelled is a legal owner transition). The server refuses a trip a
 * reviewer returned (SUBJECT_UNDER_REVIEW); the UI offers it only for drafts
 * with no approval history (UAT2 defect E follow-up).
 */
export function useDiscardTrip() {
  const qc = useQueryClient();
  return useMutation<Trip, unknown, { id: string }>({
    mutationFn: ({ id }) =>
      api.patch<Trip>(`/trips/${id}`, { body: { status: "cancelled" } }),
    onSuccess: (trip) => {
      qc.setQueryData(tripQueryKeys.trip(trip.id), trip);
      qc.invalidateQueries({ queryKey: ["trips"] });
    },
  });
}

/**
 * Submit (or, after "request changes", RE-submit) a draft trip for approval —
 * PATCH /trips/{id} {status: "pending_approval"} (C7R). A resubmission re-queues
 * the trip's existing approval for a fresh human decision on the backend.
 */
export function useSubmitTrip() {
  const qc = useQueryClient();
  return useMutation<Trip, unknown, { id: string }>({
    mutationFn: ({ id }) =>
      api.patch<Trip>(`/trips/${id}`, { body: { status: "pending_approval" } }),
    onSuccess: (trip) => {
      qc.setQueryData(tripQueryKeys.trip(trip.id), trip);
      qc.invalidateQueries({ queryKey: ["trips"] });
    },
  });
}
