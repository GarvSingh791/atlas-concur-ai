"use client";

/**
 * Product gap 2 — add a personal-vehicle mileage entry.
 *
 *   useAddMileage → POST /expense-reports/{id}/mileage  (If-Match: report version)
 *   useMileageRate → GET /mileage-rate  (the MIL-01 rate, for the live estimate)
 *
 * The body carries the trip only — never an amount or a rate: the server
 * computes amount = business miles × the MIL-01 rate, re-runs the pre-submit
 * check and bumps the report version. On settle the report, its entries and
 * the list are refetched (the client never recomputes them).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { ExpenseEntry, MileageEntryCreateRequest } from "@/lib/types";

/** GET /mileage-rate — the rate the server charges (Decimal as a string). */
export interface MileageRate {
  rate_per_mile: string;
  currency: string;
  rate_clause: string;
}

/**
 * Follow-up (UI polish): the organisation's MIL-01 rate, read-only, for the
 * Add-mileage dialog's live estimate. Display only — the server computes and
 * stores the amount on POST. Refetched on mount so a rate change shows up.
 */
export function useMileageRate(enabled = true) {
  return useQuery({
    queryKey: ["mileage-rate"],
    queryFn: () => api.get<MileageRate>("/mileage-rate"),
    staleTime: 60 * 1000,
    enabled,
  });
}

export interface AddMileageArgs {
  reportId: string;
  reportVersion: number;
  body: MileageEntryCreateRequest;
}

export function useAddMileage() {
  const qc = useQueryClient();
  return useMutation<ExpenseEntry, unknown, AddMileageArgs>({
    mutationFn: ({ reportId, reportVersion, body }) =>
      api.post<ExpenseEntry>(`/expense-reports/${reportId}/mileage`, {
        body,
        headers: { "If-Match": String(reportVersion) },
      }),
    onSettled: (_data, _err, { reportId }) => {
      qc.invalidateQueries({ queryKey: ["expense-report", reportId] });
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}
