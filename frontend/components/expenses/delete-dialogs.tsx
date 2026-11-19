"use client";

import * as React from "react";

import { useQueryClient } from "@tanstack/react-query";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import {
  forgetDeletedReport,
  useDeleteEntry,
  useDeleteReport,
} from "@/lib/query/expense-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import { CATEGORY_LABELS, formatCurrency } from "@/lib/expenses/format";
import type { ExpenseEntry, ExpenseReport } from "@/lib/types";

/**
 * Delete confirmations — UAT2.
 *
 * In-page destructive dialogs (never `window.confirm`). Mount them only while
 * open: they own their mutation hooks. The server decides what may be deleted
 * (owner, draft, never-reviewed report; a reviewed entry only when its return
 * kept a snapshot) and its refusal message is shown verbatim.
 */

function entryName(entry: ExpenseEntry): string {
  return (
    entry.vendor ?? entry.description ?? `${CATEGORY_LABELS[entry.category]}`
  );
}

export interface DeleteEntryDialogProps {
  entry: ExpenseEntry;
  report: Pick<ExpenseReport, "id" | "title" | "version" | "approval_status">;
  onOpenChange: (open: boolean) => void;
  onDeleted?: () => void;
}

export function DeleteEntryDialog({
  entry,
  report,
  onOpenChange,
  onDeleted,
}: DeleteEntryDialogProps) {
  const del = useDeleteEntry();
  const { showToast } = useAtlasToast();
  const reviewed = Boolean(report.approval_status);

  const handleConfirm = () => {
    if (del.isPending) return;
    del.mutate(
      { entryId: entry.id, reportId: report.id, reportVersion: report.version },
      {
        onSuccess: (res) => {
          showToast({
            variant: "success",
            title: "Entry deleted",
            description:
              res.retained_receipt_ids.length > 0
                ? "The pre-submit check has been re-run. Its receipt is kept for audit."
                : "The pre-submit check has been re-run.",
          });
          onOpenChange(false);
          onDeleted?.();
        },
        onError: (err) => {
          showToast(expenseErrorToast(err, "Couldn’t delete the entry"));
          onOpenChange(false);
        },
      },
    );
  };

  return (
    <Modal
      open
      variant="destructive"
      onOpenChange={onOpenChange}
      title="Delete this entry?"
      description={`“${entryName(entry)} · ${formatCurrency(entry.amount, entry.currency)}” will be removed from “${report.title}”. ${
        reviewed
          ? "Its receipt is kept for audit because this report has been reviewed."
          : "Its receipt is deleted too."
      } This can’t be undone.`}
      confirmLabel={del.isPending ? "Deleting…" : "Delete entry"}
      onConfirm={handleConfirm}
    />
  );
}

export interface DeleteReportDialogProps {
  report: Pick<ExpenseReport, "id" | "title" | "version">;
  entryCount: number;
  onOpenChange: (open: boolean) => void;
  /** Called after a successful delete — navigate away from the report. */
  onDeleted: () => void;
}

export function DeleteReportDialog({
  report,
  entryCount,
  onOpenChange,
  onDeleted,
}: DeleteReportDialogProps) {
  const del = useDeleteReport();
  const qc = useQueryClient();
  const { showToast } = useAtlasToast();

  const handleConfirm = () => {
    if (del.isPending) return;
    del.mutate(
      { id: report.id, version: report.version },
      {
        onSuccess: () => {
          showToast({
            variant: "success",
            title: "Report deleted",
            description: `“${report.title}” has been deleted.`,
          });
          onOpenChange(false);
          // Navigate away first, then drop the (now 404) detail from the cache.
          onDeleted();
          forgetDeletedReport(qc, report.id);
        },
        onError: (err) => {
          showToast(expenseErrorToast(err, "Couldn’t delete the report"));
          onOpenChange(false);
        },
      },
    );
  };

  const what =
    entryCount === 0
      ? "This empty draft"
      : `This draft and its ${entryCount} ${entryCount === 1 ? "entry" : "entries"} (with their receipts)`;

  return (
    <Modal
      open
      variant="destructive"
      onOpenChange={onOpenChange}
      title={`Delete “${report.title}”?`}
      description={`${what} will be permanently deleted. This can’t be undone.`}
      confirmLabel={del.isPending ? "Deleting…" : "Delete report"}
      onConfirm={handleConfirm}
    />
  );
}
