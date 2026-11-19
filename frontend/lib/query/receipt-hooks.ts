"use client";

/**
 * Receipt mutation hooks — P5-T6.
 *
 * Two server interactions, both synchronous from the UI's point of view:
 *   - useUploadReceipt: multipart POST /receipts with an Idempotency-Key header.
 *     The response already carries the extracted fields (no polling).
 *   - useCorrectReceiptField: PATCH /receipts/{id} with { field, value } ONLY.
 *     The SERVER flips field_provenance[field]='user_edited' — the client never
 *     sends provenance. The returned receipt is the authoritative re-render source.
 *
 * UAT2:
 *   - useAttachReceipt: POST /receipts?expense_id= — upload a receipt straight
 *     onto a draft entry (If-Match = report version). The server re-runs the
 *     pre-submit check and bumps the version; every expense cache refreshes.
 *   - useAttachableReceipts: GET /receipts — the caller's unlinked receipts.
 *   - useDeleteReceipt: DELETE /receipts/{id}.
 *
 * Read hook (C2, NFR-SEC4):
 *   - useReceipt: GET /receipts/{id}. The response carries a short-lived signed
 *     `image_access_url` (null when the server cannot mint a browser-servable
 *     link, e.g. local storage). staleTime is 0 — the shared client's 5-min
 *     staleTime outlives the 300 s link TTL — and the query refetches shortly
 *     before the link expires (arrival time + `image_access_expires_in`). The URL is never logged or persisted.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from "@tanstack/react-query";
import { api } from "@/lib/api/client";
import { msUntilRefresh } from "@/lib/receipts/image-access";
import { isApiError } from "@/lib/types";
import type {
  Receipt,
  ReceiptCorrection,
  ReceiptDeleteResponse,
} from "@/lib/types";

export const receiptKeys = {
  detail: (id: string) => ["receipt", id] as const,
  attachable: (forEntryId?: string) =>
    forEntryId
      ? (["receipts", "attachable", forEntryId] as const)
      : (["receipts", "attachable"] as const),
};

/**
 * C7R: an upload can auto-draft an expense into the Receipt Inbox, and a PATCH
 * can create that draft (entering a withheld total) or change a linked entry and
 * its report total. The shared client's 5-min staleTime would otherwise keep
 * showing the old reports / entries / totals, so every receipt mutation that
 * touched an expense refreshes the expense-report caches (and the dashboard
 * analytics that count them).
 */
export function receiptTouchedExpenses(receipt: Receipt): boolean {
  if (receipt.policy_verdict) return true;
  const sync = receipt.linked_expense_sync;
  return sync === "drafted" || sync === "updated" || sync === "attached";
}

function refreshExpenseCaches(qc: QueryClient, receipt: Receipt): void {
  if (!receiptTouchedExpenses(receipt)) return;
  void qc.invalidateQueries({ queryKey: ["expense-reports"] });
  void qc.invalidateQueries({ queryKey: ["expense-report"] });
  void qc.invalidateQueries({ queryKey: ["analytics"] });
}

export function useReceipt(id: string | null) {
  return useQuery({
    queryKey: id ? receiptKeys.detail(id) : ["receipt", "none"],
    queryFn: () => api.get<Receipt>(`/receipts/${id}`),
    enabled: Boolean(id),
    // Never serve a cached (possibly expired) signed link.
    staleTime: 0,
    gcTime: 60_000,
    // A 4xx (out of scope / not found) is definitive — don't hammer it.
    retry: (failureCount, error) =>
      !(isApiError(error) && error.status >= 400 && error.status < 500) &&
      failureCount < 2,
    // Anchored on when the response arrived + the relative lifetime, so a
    // skewed browser clock cannot cause a refetch storm (see image-access.ts).
    refetchInterval: (query) =>
      msUntilRefresh(query.state.data, query.state.dataUpdatedAt),
  });
}

export interface UploadReceiptArgs {
  file: File;
  /** Stable per-file UUID so retries don't create duplicates (NFR-R3). */
  idempotencyKey: string;
}

export function useUploadReceipt() {
  const qc = useQueryClient();
  return useMutation<Receipt, unknown, UploadReceiptArgs>({
    onSuccess: (receipt) => refreshExpenseCaches(qc, receipt),
    mutationFn: ({ file, idempotencyKey }) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.post<Receipt>("/receipts", {
        body: formData,
        multipart: true,
        headers: { "Idempotency-Key": idempotencyKey },
      });
    },
  });
}

export interface CorrectReceiptArgs extends ReceiptCorrection {
  receiptId: string;
}

export function useCorrectReceiptField() {
  const qc = useQueryClient();
  return useMutation<Receipt, unknown, CorrectReceiptArgs>({
    onSuccess: (receipt) => {
      qc.setQueryData<Receipt | undefined>(
        receiptKeys.detail(receipt.id),
        (cached) =>
          cached
            ? // Keep the cached signed image link; PATCH never carries one.
              {
                ...receipt,
                image_access_url: cached.image_access_url,
                image_access_expires_at: cached.image_access_expires_at,
                image_access_expires_in: cached.image_access_expires_in,
              }
            : cached,
      );
      refreshExpenseCaches(qc, receipt);
    },
    mutationFn: ({ receiptId, field, value }) =>
      // Body is value-only — the server owns the provenance flip.
      api.patch<Receipt>(`/receipts/${receiptId}`, {
        body: { field, value },
      }),
  });
}

// ---------------------------------------------------------------------------
// UAT2 — attach a receipt to an entry / delete a receipt
// ---------------------------------------------------------------------------

function refreshAfterReceiptChange(qc: QueryClient): void {
  // Duplicate peers on OTHER reports may have been re-checked too.
  void qc.invalidateQueries({ queryKey: ["expense-reports"] });
  void qc.invalidateQueries({ queryKey: ["expense-report"] });
  void qc.invalidateQueries({ queryKey: ["analytics"] });
  void qc.invalidateQueries({ queryKey: receiptKeys.attachable() });
}

export interface AttachReceiptArgs {
  entryId: string;
  reportId: string;
  /** Parent report version (If-Match). */
  reportVersion: number;
  file: File;
  /** Stable per-attempt UUID so a retry doesn't attach twice (NFR-R3). */
  idempotencyKey: string;
}

export function useAttachReceipt() {
  const qc = useQueryClient();
  return useMutation<Receipt, unknown, AttachReceiptArgs>({
    mutationFn: ({ entryId, reportVersion, file, idempotencyKey }) => {
      const formData = new FormData();
      formData.append("file", file);
      return api.post<Receipt>("/receipts", {
        params: { expense_id: entryId },
        body: formData,
        multipart: true,
        headers: {
          "Idempotency-Key": idempotencyKey,
          "If-Match": String(reportVersion),
        },
      });
    },
    // Also on error: a 409 means our copy of the report is stale.
    onSettled: () => refreshAfterReceiptChange(qc),
  });
}

/**
 * GET /receipts — the caller's attachable receipts. With `forEntryId` (UAT2
 * hardening) the server also offers receipts on OTHER lines of that entry's
 * draft report, so one itemized receipt (a hotel folio) can be split across
 * lines; the link re-checks that the lines stay within its total.
 */
export function useAttachableReceipts(forEntryId?: string, enabled = true) {
  return useQuery({
    queryKey: receiptKeys.attachable(forEntryId),
    queryFn: () =>
      api.get<Receipt[]>("/receipts", {
        params: forEntryId ? { for_expense_id: forEntryId } : undefined,
      }),
    enabled,
    staleTime: 0,
  });
}

export interface DeleteReceiptArgs {
  receiptId: string;
  /** Also delete the entry the upload auto-drafted from this receipt. */
  discardDraftedEntry?: boolean;
  /**
   * UAT2 hardening: remove the receipt from THIS line only. A receipt split
   * across several lines (itemized siblings) keeps backing the others.
   */
  fromEntryId?: string;
}

export function useDeleteReceipt() {
  const qc = useQueryClient();
  return useMutation<ReceiptDeleteResponse, unknown, DeleteReceiptArgs>({
    mutationFn: ({ receiptId, discardDraftedEntry, fromEntryId }) => {
      const params: Record<string, string> = {};
      if (discardDraftedEntry) params.discard_drafted_entry = "true";
      if (fromEntryId) params.from_expense_id = fromEntryId;
      return api.delete<ReceiptDeleteResponse>(`/receipts/${receiptId}`, {
        params: Object.keys(params).length > 0 ? params : undefined,
      });
    },
    onSuccess: (_res, { receiptId }) => {
      qc.removeQueries({ queryKey: receiptKeys.detail(receiptId) });
    },
    onSettled: () => refreshAfterReceiptChange(qc),
  });
}
