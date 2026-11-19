"use client";

/**
 * Approval queue + decision hooks — P5-T8.
 *
 * Wired to the REAL contract (backend/app/modules/approval/router.py):
 *   - useApprovals       → GET   /approvals               (role-scoped, paginated)
 *   - useApproval        → GET   /approvals/{id}          (decision detail)
 *   - useDecideApproval  → PATCH /approvals/{id}          { decision, reason }
 *                          with an `If-Match: <version>` header for optimistic
 *                          concurrency. A stale version surfaces as a 409 CONFLICT
 *                          ApiError (no silent overwrite).
 *
 * C7R robustness:
 *   - reads retry transient failures (network, 502/503/504) with backoff — set
 *     on the hooks themselves so the policy holds whatever client mounts them;
 *   - a decision updates the panel + queue from the PATCH response at once and
 *     then invalidates every view the decision changes (queue, detail,
 *     dashboard counts, the subject report/trip). On ANY failure the views are
 *     re-synced too: a lost response may still have committed server-side.
 *
 * The screen renders server truth (risk_summary + the scoped queue) and triggers
 * decisions the server commits + audits. It NEVER scores risk or filters for
 * authority — scoping is server-enforced (scope_query).
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import { retryTransient, transientRetryDelay } from "@/lib/query/retry";
import type { PaginatedResponse } from "@/lib/types/api";
import type {
  ApprovalDecisionValue,
  ApprovalDetail,
  ApprovalListItem,
  ApprovalListParams,
} from "@/lib/types/approvals";

export const approvalKeys = {
  list: (params?: ApprovalListParams) => ["approvals", params ?? {}] as const,
  detail: (id: string) => ["approval", id] as const,
};

export function useApprovals(
  params?: ApprovalListParams,
  options?: { enabled?: boolean },
) {
  return useQuery({
    enabled: options?.enabled ?? true,
    queryKey: approvalKeys.list(params),
    queryFn: () =>
      api.get<PaginatedResponse<ApprovalListItem>>("/approvals", {
        params: params as Record<string, string | number | boolean | undefined>,
      }),
    retry: retryTransient,
    retryDelay: (attempt) => transientRetryDelay(attempt),
  });
}

export function useApproval(id: string | null) {
  return useQuery({
    queryKey: id ? approvalKeys.detail(id) : ["approval", "none"],
    queryFn: () => api.get<ApprovalDetail>(`/approvals/${id}`),
    enabled: Boolean(id),
    retry: retryTransient,
    retryDelay: (attempt) => transientRetryDelay(attempt),
  });
}

export interface DecideApprovalArgs {
  id: string;
  decision: Exclude<ApprovalDecisionValue, "auto_approve">;
  reason?: string | null;
  /** Current version for optimistic concurrency (If-Match). */
  version: number;
}

export function useDecideApproval() {
  const qc = useQueryClient();
  return useMutation<ApprovalDetail, unknown, DecideApprovalArgs>({
    mutationFn: ({ id, decision, reason, version }) =>
      api.patch<ApprovalDetail>(`/approvals/${id}`, {
        body: { decision, reason: reason ?? null },
        headers: { "If-Match": String(version) },
      }),
    onSuccess: async (detail) => {
      // 1) Show the decision immediately from the PATCH response. Cancel any
      //    in-flight detail GET first so a slower, pre-decision response cannot
      //    land afterwards and flip the panel back to "Pending".
      await qc.cancelQueries({ queryKey: approvalKeys.detail(detail.id) });
      qc.setQueryData<ApprovalDetail>(approvalKeys.detail(detail.id), (prev) =>
        prev && prev.version > detail.version ? prev : detail,
      );
      // 2) Patch the row in every cached queue page so the list badge moves now.
      qc.setQueriesData<PaginatedResponse<ApprovalListItem>>(
        { queryKey: ["approvals"] },
        (page) =>
          page && Array.isArray(page.items)
            ? {
                ...page,
                items: page.items.map((it) =>
                  it.id === detail.id
                    ? {
                        ...it,
                        current_status: detail.current_status,
                        is_auto_approved: detail.is_auto_approved,
                        version: detail.version,
                      }
                    : it,
                ),
              }
            : page,
      );
    },
    onSettled: (_data, _err, { id }) => {
      // 3) Reconcile everything the decision touches with server truth — on
      //    success AND failure (a 409/400 means the cache is stale; a network
      //    error may hide a decision that DID commit).
      invalidateDecisionViews(qc, id);
    },
  });
}

/** Every cached view an approval decision can change. */
export function invalidateDecisionViews(
  qc: ReturnType<typeof useQueryClient>,
  approvalId: string,
): void {
  void qc.invalidateQueries({ queryKey: approvalKeys.detail(approvalId) });
  void qc.invalidateQueries({ queryKey: ["approvals"] });
  // Dashboard counts / queue widget (analytics overview) and the subject
  // report or trip, whose status moves with the decision.
  void qc.invalidateQueries({ queryKey: ["analytics"] });
  void qc.invalidateQueries({ queryKey: ["expense-reports"] });
  void qc.invalidateQueries({ queryKey: ["expense-report"] });
  void qc.invalidateQueries({ queryKey: ["trips"] });
}
