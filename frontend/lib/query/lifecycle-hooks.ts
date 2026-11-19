"use client";

/**
 * Report & trip lifecycle hooks — product gaps 1, 4, 8.
 *
 * Wired to backend/app/modules/lifecycle/router.py:
 *   - useReimbursementQueue → GET  /reimbursements/queue            (finance)
 *   - useReimburseReport    → POST /expense-reports/{id}/reimburse   (finance)
 *   - useWithdrawReport     → POST /expense-reports/{id}/withdraw    (owner)
 *   - useWithdrawTrip       → POST /trips/{id}/withdraw              (traveler)
 *   - useCompleteTrip       → POST /trips/{id}/complete              (traveler / finance)
 *
 * The server decides everything (permission, owner, state, SoD); the UI only
 * hides actions it would refuse. Report writes carry the version the user last
 * saw as If-Match, so a stale screen gets a 409 instead of a silent change.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import { expenseKeys } from "@/lib/query/expense-hooks";
import { tripQueryKeys } from "@/lib/query/trip-hooks";
import type { ExpenseReport, Trip } from "@/lib/types";
import type { ReimbursementQueue } from "@/lib/types/lifecycle";

export const lifecycleKeys = {
  reimbursementQueue: () => ["reimbursements", "queue"] as const,
};

function refreshReports(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: ["expense-report"] });
  void qc.invalidateQueries({ queryKey: ["expense-reports"] });
  void qc.invalidateQueries({ queryKey: ["reimbursements"] });
  void qc.invalidateQueries({ queryKey: ["analytics"] });
}

/** Approved reports awaiting (simulated) reimbursement. Enable for finance only. */
export function useReimbursementQueue(enabled = true) {
  return useQuery<ReimbursementQueue>({
    queryKey: lifecycleKeys.reimbursementQueue(),
    queryFn: () => api.get<ReimbursementQueue>("/reimbursements/queue"),
    enabled,
  });
}

export interface ReimburseArgs {
  id: string;
  version: number;
  note?: string;
}

export function useReimburseReport() {
  const qc = useQueryClient();
  return useMutation<ExpenseReport, unknown, ReimburseArgs>({
    mutationFn: ({ id, version, note }) =>
      api.post<ExpenseReport>(`/expense-reports/${id}/reimburse`, {
        body: note ? { note } : {},
        headers: { "If-Match": String(version) },
      }),
    onSuccess: (report) => {
      qc.setQueryData(expenseKeys.detail(report.id), report);
    },
    onSettled: () => refreshReports(qc),
  });
}

export interface WithdrawReportArgs {
  id: string;
  version: number;
  reason?: string;
}

export function useWithdrawReport() {
  const qc = useQueryClient();
  return useMutation<ExpenseReport, unknown, WithdrawReportArgs>({
    mutationFn: ({ id, version, reason }) =>
      api.post<ExpenseReport>(`/expense-reports/${id}/withdraw`, {
        body: reason ? { reason } : {},
        headers: { "If-Match": String(version) },
      }),
    onSuccess: (report) => {
      qc.setQueryData(expenseKeys.detail(report.id), report);
    },
    onSettled: () => refreshReports(qc),
  });
}

function refreshTrips(qc: QueryClient, trip?: Trip): void {
  if (trip) qc.setQueryData(tripQueryKeys.trip(trip.id), trip);
  void qc.invalidateQueries({ queryKey: ["trips"] });
  void qc.invalidateQueries({ queryKey: ["analytics"] });
}

export function useWithdrawTrip() {
  const qc = useQueryClient();
  return useMutation<Trip, unknown, { id: string; reason?: string }>({
    mutationFn: ({ id, reason }) =>
      api.post<Trip>(`/trips/${id}/withdraw`, {
        body: reason ? { reason } : {},
      }),
    onSuccess: (trip) => refreshTrips(qc, trip),
    onError: () => refreshTrips(qc),
  });
}

export function useCompleteTrip() {
  const qc = useQueryClient();
  return useMutation<Trip, unknown, { id: string }>({
    mutationFn: ({ id }) => api.post<Trip>(`/trips/${id}/complete`),
    onSuccess: (trip) => refreshTrips(qc, trip),
    onError: () => refreshTrips(qc),
  });
}
