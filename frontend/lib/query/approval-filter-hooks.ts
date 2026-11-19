"use client";

/**
 * Approval filter-option hooks (Product gap 6):
 *   - useExceptionTypes       → GET /approvals/exception-types (scoped counts
 *                               under the other active filters — follow-up)
 *   - useApprovalDepartments  → GET /approvals/departments
 * Options change rarely; cached for 5 minutes.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { ExceptionTypeOption } from "@/lib/types/approval-filters";
import type { ApprovalFilters } from "@/lib/approvals/filter-params";

const FIVE_MINUTES = 5 * 60 * 1000;

export const approvalFilterKeys = {
  exceptionTypes: (filters: Record<string, string> = {}) =>
    ["approvals", "filter-options", "exception-types", filters] as const,
  departments: () => ["approvals", "filter-options", "departments"] as const,
};

/**
 * The OTHER active filters the counts are taken under: the selected exception
 * type is left out (faceted counts — each option says how many rows choosing
 * it would show), blanks are dropped.
 */
export function exceptionCountParams(
  filters: ApprovalFilters = {},
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(filters)) {
    const v = value?.trim();
    if (key !== "exception_type" && v) out[key] = v;
  }
  return out;
}

export function useExceptionTypes(filters?: ApprovalFilters, enabled = true) {
  const params = exceptionCountParams(filters);
  return useQuery({
    queryKey: approvalFilterKeys.exceptionTypes(params),
    queryFn: () =>
      api.get<ExceptionTypeOption[]>("/approvals/exception-types", {
        params,
      }),
    staleTime: FIVE_MINUTES,
    // Keep the old counts on screen while the new filter set loads.
    placeholderData: keepPreviousData,
    enabled,
  });
}

export function useApprovalDepartments(enabled = true) {
  return useQuery({
    queryKey: approvalFilterKeys.departments(),
    queryFn: () => api.get<string[]>("/approvals/departments"),
    staleTime: FIVE_MINUTES,
    enabled,
  });
}
