"use client";

/**
 * Audit trail query hook — C4.
 *
 * Wired to the REAL contract (backend/app/modules/audit/router.py):
 *   - useAuditLog → GET /audit   (org-scoped, finance_admin only, paginated)
 *
 * Read-only: the audit log is append-only and written exclusively server-side
 * (record_audit), so there are no mutations here. Empty / undefined filter
 * values are dropped so the server never receives a blank exact-match filter.
 */

import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type { PaginatedResponse } from "@/lib/types/api";
import type { AuditListParams, AuditLogItem } from "@/lib/types/audit";

/** Drop undefined / blank values so they never reach the query string. */
export function cleanAuditParams(params?: AuditListParams): AuditListParams {
  const out: AuditListParams = {};
  if (!params) return out;
  for (const [key, value] of Object.entries(params) as [
    keyof AuditListParams,
    string | number | undefined,
  ][]) {
    if (value === undefined) continue;
    if (typeof value === "string" && value.trim() === "") continue;
    (out as Record<string, string | number>)[key] =
      typeof value === "string" ? value.trim() : value;
  }
  return out;
}

export const auditKeys = {
  list: (params?: AuditListParams) =>
    ["audit-log", cleanAuditParams(params)] as const,
};

export function useAuditLog(params?: AuditListParams) {
  const cleaned = cleanAuditParams(params);
  return useQuery({
    queryKey: auditKeys.list(cleaned),
    queryFn: () =>
      api.get<PaginatedResponse<AuditLogItem>>("/audit", {
        params: cleaned as Record<string, string | number | undefined>,
      }),
    // Keep the current page on screen while the next page / filter loads.
    placeholderData: keepPreviousData,
  });
}
