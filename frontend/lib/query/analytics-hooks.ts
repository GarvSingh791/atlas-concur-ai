/**
 * Analytics query hooks — GET /api/v1/analytics/overview
 * GET /api/v1/trips (trip list for trip timeline widget)
 * GET /api/v1/expense-reports (expense list for recent expenses widget)
 */

"use client";

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import type { AnalyticsOverviewResponse } from "@/lib/types/analytics";

// ---------------------------------------------------------------------------
// Query keys
// ---------------------------------------------------------------------------

export const analyticsQueryKeys = {
  overview: ["analytics", "overview"] as const,
  // "mine": the employee dashboard widgets show the caller's OWN items (UAT2).
  trips: (params?: object) => ["trips", "mine", params] as const,
  expenseReports: (params?: object) =>
    ["expense-reports", "mine", params] as const,
};

// ---------------------------------------------------------------------------
// useAnalyticsOverview — GET /api/v1/analytics/overview
// ---------------------------------------------------------------------------

/**
 * Fetches the role-scoped analytics overview. The server returns exactly one
 * populated section (employee | manager | finance) based on the caller's role.
 * A single request dedupes across all widgets that read from this hook.
 */
export function useAnalyticsOverview() {
  return useQuery<AnalyticsOverviewResponse>({
    queryKey: analyticsQueryKeys.overview,
    queryFn: () => api.get<AnalyticsOverviewResponse>("/analytics/overview"),
  });
}

// ---------------------------------------------------------------------------
// useTrips — GET /api/v1/trips (paginated)
// ---------------------------------------------------------------------------

/**
 * Minimal read hook for the trip timeline widget — the caller's OWN trips.
 * Sends `mine=true` (UAT2) so the widget never shows a team scope, even if an
 * authenticated manager briefly renders the employee dashboard while the role
 * is still resolving.
 */
export function useTrips(params?: { page?: number; size?: number }) {
  return useQuery({
    queryKey: analyticsQueryKeys.trips(params),
    queryFn: () => api.get("/trips", { params: { ...params, mine: true } }),
  });
}

// ---------------------------------------------------------------------------
// useExpenseReports — GET /api/v1/expense-reports (paginated)
// ---------------------------------------------------------------------------

/**
 * Minimal read hook for the recent expenses widget — the caller's OWN reports
 * (`mine=true`, UAT2; see useTrips above).
 */
export function useExpenseReports(params?: { page?: number; size?: number }) {
  return useQuery({
    queryKey: analyticsQueryKeys.expenseReports(params),
    queryFn: () =>
      api.get("/expense-reports", { params: { ...params, mine: true } }),
  });
}
