"use client";

/**
 * Expense report query + mutation hooks — P5-T7.
 *
 * Wired to the REAL contract (backend/app/modules/expense/router.py):
 *   - useExpenseReports  → GET  /expense-reports?mine=true (the caller's OWN
 *                          reports, paginated — UAT2: a manager's personal list
 *                          never shows the team; team review lives on /approvals)
 *   - useExpenseReport   → GET  /expense-reports/{id}
 *   - useReportEntries   → GET  /expense-reports/{id}/entries
 *   - useSubmitReport    → PATCH /expense-reports/{id}  { status:"submitted" }
 *                          with an `If-Match: <version>` header for optimistic
 *                          concurrency. A stale version surfaces as a 409
 *                          CONFLICT ApiError (no silent overwrite).
 *   - useUpdateEntry     → PATCH /expenses/{id}               (C7R UAT)
 *   - useCreateReport    → POST  /expense-reports             (C7R UAT)
 *   - useRenameReport    → PATCH /expense-reports/{id}/details (C7R UAT)
 *   - useMoveEntries     → POST  /expense-reports/{id}/entries/move (C7R UAT)
 *   - useDeleteEntry     → DELETE /expenses/{id}                  (UAT2)
 *   - useDeleteReport    → DELETE /expense-reports/{id}           (UAT2)
 *   - useLinkReceipt     → POST  /expenses/{id}/receipts          (UAT2)
 *
 * The screen never writes flags, totals, or match data — it edits entry fields
 * and the server re-derives totals and re-runs the pre-submit check.
 */

import { keepCallerItems } from "@/lib/query/own-items";
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";

import { api } from "@/lib/api/client";
import type {
  EntryDeleteResponse,
  ExpenseEntry,
  ExpenseEntryUpdate,
  ExpenseReport,
  ExpenseReportCreateRequest,
  MoveEntriesResponse,
  ReceiptLinkResponse,
  ReportListItem,
  ReportListParams,
} from "@/lib/types";
import type { PaginatedResponse } from "@/lib/types/api";

export const expenseKeys = {
  // "mine" segment: the personal list. Keys carry no user id — logout clears
  // the query cache (useLogout → qc.clear()), so an in-place account switch
  // must clear it too.
  list: (params?: ReportListParams) =>
    ["expense-reports", "mine", params ?? {}] as const,
  detail: (id: string) => ["expense-report", id] as const,
  entries: (id: string) => ["expense-report", id, "entries"] as const,
};

/**
 * The caller's OWN reports (personal "Expenses" page, move-entries targets).
 * Always sends `mine=true`: without it the server returns the role's full read
 * scope, so a manager's list would include their direct reports' reports.
 */
export function useExpenseReports(params?: Omit<ReportListParams, "mine">) {
  return useQuery({
    queryKey: expenseKeys.list(params),
    queryFn: async () =>
      keepCallerItems(
        await api.get<PaginatedResponse<ReportListItem>>("/expense-reports", {
          params: { ...params, mine: true } as Record<
            string,
            string | number | boolean | undefined
          >,
        }),
        (r) => r.owner_id,
      ),
  });
}

export function useExpenseReport(id: string | null) {
  return useQuery({
    queryKey: id ? expenseKeys.detail(id) : ["expense-report", "none"],
    queryFn: () => api.get<ExpenseReport>(`/expense-reports/${id}`),
    enabled: Boolean(id),
  });
}

/**
 * Fetch the report fresh from the server (bypassing the 5-min staleTime) and
 * prime the detail cache — UAT2 hardening: the attach toast reads the REAL
 * refreshed submit gate, not a cached copy from before the server's re-check.
 */
export function fetchFreshReport(
  qc: QueryClient,
  id: string,
): Promise<ExpenseReport> {
  return qc.fetchQuery({
    queryKey: expenseKeys.detail(id),
    queryFn: () => api.get<ExpenseReport>(`/expense-reports/${id}`),
    staleTime: 0,
  });
}

export function useReportEntries(id: string | null) {
  return useQuery({
    queryKey: id
      ? expenseKeys.entries(id)
      : ["expense-report", "none", "entries"],
    queryFn: () => api.get<ExpenseEntry[]>(`/expense-reports/${id}/entries`),
    enabled: Boolean(id),
  });
}

export interface SubmitReportArgs {
  id: string;
  /** Current version for optimistic concurrency (If-Match). */
  version: number;
}

export function useSubmitReport() {
  const qc = useQueryClient();
  return useMutation<ExpenseReport, unknown, SubmitReportArgs>({
    mutationFn: ({ id, version }) =>
      api.patch<ExpenseReport>(`/expense-reports/${id}`, {
        body: { status: "submitted" },
        headers: { "If-Match": String(version) },
      }),
    onSuccess: (report) => {
      // Refresh the detail + list so the new status/version propagate.
      qc.setQueryData(expenseKeys.detail(report.id), report);
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}

// ---------------------------------------------------------------------------
// C7R UAT — fix a returned report / assemble reports. Every write carries the
// report version the user last saw as `If-Match`; a stale version surfaces as a
// 409 CONFLICT ApiError. The server re-runs the pre-submit check on each entry
// edit / move, so on success the report detail (flags, total, version), its
// entries and the list are refetched — the client never recomputes them.
// ---------------------------------------------------------------------------

export interface UpdateEntryArgs {
  entryId: string;
  reportId: string;
  /** Parent report version (entries carry none). */
  reportVersion: number;
  changes: ExpenseEntryUpdate;
}

export function useUpdateEntry() {
  const qc = useQueryClient();
  return useMutation<ExpenseEntry, unknown, UpdateEntryArgs>({
    mutationFn: ({ entryId, reportVersion, changes }) =>
      api.patch<ExpenseEntry>(`/expenses/${entryId}`, {
        body: changes,
        headers: { "If-Match": String(reportVersion) },
      }),
    onSettled: (_data, _err, { reportId }) => {
      // Also on error: a 409 means our copy is stale — refetch it.
      qc.invalidateQueries({ queryKey: ["expense-report", reportId] });
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}

export function useCreateReport() {
  const qc = useQueryClient();
  return useMutation<ExpenseReport, unknown, ExpenseReportCreateRequest>({
    mutationFn: (body) => api.post<ExpenseReport>("/expense-reports", { body }),
    onSuccess: (report) => {
      qc.setQueryData(expenseKeys.detail(report.id), report);
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}

export interface RenameReportArgs {
  id: string;
  version: number;
  title: string;
}

export function useRenameReport() {
  const qc = useQueryClient();
  return useMutation<ExpenseReport, unknown, RenameReportArgs>({
    mutationFn: ({ id, version, title }) =>
      api.patch<ExpenseReport>(`/expense-reports/${id}/details`, {
        body: { title },
        headers: { "If-Match": String(version) },
      }),
    onSuccess: (report) => {
      qc.setQueryData(expenseKeys.detail(report.id), report);
    },
    onSettled: (_data, _err, { id }) => {
      qc.invalidateQueries({ queryKey: expenseKeys.detail(id) });
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}

export interface MoveEntriesArgs {
  /** The report the entries are on now (its version goes in If-Match). */
  sourceId: string;
  sourceVersion: number;
  targetId: string;
  entryIds: string[];
}

export function useMoveEntries() {
  const qc = useQueryClient();
  return useMutation<MoveEntriesResponse, unknown, MoveEntriesArgs>({
    mutationFn: ({ sourceId, sourceVersion, targetId, entryIds }) =>
      api.post<MoveEntriesResponse>(
        `/expense-reports/${sourceId}/entries/move`,
        {
          body: { entry_ids: entryIds, target_report_id: targetId },
          headers: { "If-Match": String(sourceVersion) },
        },
      ),
    onSettled: (_data, _err, { sourceId, targetId }) => {
      qc.invalidateQueries({ queryKey: ["expense-report", sourceId] });
      qc.invalidateQueries({ queryKey: ["expense-report", targetId] });
      qc.invalidateQueries({ queryKey: ["expense-reports"] });
    },
  });
}

// ---------------------------------------------------------------------------
// UAT2 — delete entries / never-submitted reports, attach an existing receipt.
// The server also re-checks OTHER draft reports whose duplicate flag named the
// changed line (and bumps their version), so the WHOLE expense-report cache is
// refreshed — a cached peer would otherwise show a stale blocking flag and send
// a stale If-Match on submit.
// ---------------------------------------------------------------------------

export function refreshAllExpenseCaches(qc: QueryClient): void {
  void qc.invalidateQueries({ queryKey: ["expense-report"] });
  void qc.invalidateQueries({ queryKey: ["expense-reports"] });
  void qc.invalidateQueries({ queryKey: ["analytics"] });
}

export interface DeleteEntryArgs {
  entryId: string;
  reportId: string;
  /** Parent report version (If-Match). */
  reportVersion: number;
}

export function useDeleteEntry() {
  const qc = useQueryClient();
  return useMutation<EntryDeleteResponse, unknown, DeleteEntryArgs>({
    mutationFn: ({ entryId, reportVersion }) =>
      api.delete<EntryDeleteResponse>(`/expenses/${entryId}`, {
        headers: { "If-Match": String(reportVersion) },
      }),
    onSuccess: (res) => {
      qc.setQueryData(expenseKeys.detail(res.report.id), res.report);
    },
    onSettled: () => refreshAllExpenseCaches(qc),
  });
}

export interface DeleteReportArgs {
  id: string;
  version: number;
}

export function useDeleteReport() {
  const qc = useQueryClient();
  return useMutation<null, unknown, DeleteReportArgs>({
    mutationFn: ({ id, version }) =>
      api.delete<null>(`/expense-reports/${id}`, {
        headers: { "If-Match": String(version) },
      }),
    onSuccess: () => {
      // The deleted report's detail is dropped by the caller AFTER it has
      // navigated away (forgetDeletedReport) — removing it while the detail is
      // still mounted would refetch it and flash a 404 error state.
      void qc.invalidateQueries({ queryKey: ["expense-reports"] });
      void qc.invalidateQueries({ queryKey: ["analytics"] });
    },
    onError: () => refreshAllExpenseCaches(qc),
  });
}

/** Drop a deleted report's cached detail / entries (call after navigating away). */
export function forgetDeletedReport(qc: QueryClient, id: string): void {
  qc.removeQueries({ queryKey: ["expense-report", id] });
}

export interface LinkReceiptArgs {
  entryId: string;
  reportId: string;
  reportVersion: number;
  receiptId: string;
}

export function useLinkReceipt() {
  const qc = useQueryClient();
  return useMutation<ReceiptLinkResponse, unknown, LinkReceiptArgs>({
    mutationFn: ({ entryId, reportVersion, receiptId }) =>
      api.post<ReceiptLinkResponse>(`/expenses/${entryId}/receipts`, {
        body: { receipt_id: receiptId },
        headers: { "If-Match": String(reportVersion) },
      }),
    onSuccess: (res) => {
      qc.setQueryData(expenseKeys.detail(res.report.id), res.report);
    },
    onSettled: () => {
      refreshAllExpenseCaches(qc);
      void qc.invalidateQueries({ queryKey: ["receipts", "attachable"] });
    },
  });
}
