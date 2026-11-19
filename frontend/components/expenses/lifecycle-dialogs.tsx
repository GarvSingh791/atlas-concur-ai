"use client";

import * as React from "react";
import { Archive, BadgeCheck } from "lucide-react";

import { Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import { formatCurrency, formatDate } from "@/lib/expenses/format";
import {
  useReimburseReport,
  useWithdrawReport,
} from "@/lib/query/lifecycle-hooks";
import type { ExpenseReport } from "@/lib/types";

/**
 * Report lifecycle dialogs + notice — product gaps 1 and 4.
 *
 * Mount the dialogs only while open: they own their mutation hooks. The server
 * decides (owner / finance permission / state / segregation of duties) and its
 * refusal message is shown verbatim.
 */

// ---------------------------------------------------------------------------
// Withdraw (owner, returned report)
// ---------------------------------------------------------------------------

export interface WithdrawReportDialogProps {
  report: Pick<ExpenseReport, "id" | "title" | "version">;
  onOpenChange: (open: boolean) => void;
}

export function WithdrawReportDialog({
  report,
  onOpenChange,
}: WithdrawReportDialogProps) {
  const withdraw = useWithdrawReport();
  const { showToast } = useAtlasToast();

  const handleConfirm = (reason?: string) => {
    if (withdraw.isPending) return;
    withdraw.mutate(
      {
        id: report.id,
        version: report.version,
        reason: reason?.trim() || undefined,
      },
      {
        onSuccess: () => {
          onOpenChange(false);
          showToast({
            variant: "success",
            title: "Report withdrawn",
            description: `“${report.title}” is closed. Its lines can be moved to another draft.`,
          });
        },
        onError: (err) => {
          onOpenChange(false);
          showToast(expenseErrorToast(err, "Couldn’t withdraw the report"));
        },
      },
    );
  };

  return (
    <Modal
      open
      variant="reason"
      requireReason={false}
      onOpenChange={onOpenChange}
      title="Withdraw this report?"
      description={`“${report.title}” will be closed and can no longer be edited or resubmitted. This can’t be undone. Its review history stays on record, and you can move its lines to another draft report afterwards. Add an optional note for the record.`}
      confirmLabel={withdraw.isPending ? "Withdrawing…" : "Withdraw report"}
      confirmLoading={withdraw.isPending}
      onConfirm={handleConfirm}
    />
  );
}

// ---------------------------------------------------------------------------
// Mark reimbursed (finance, approved report of someone else)
// ---------------------------------------------------------------------------

export interface ReimburseReportDialogProps {
  report: {
    id: string;
    title: string;
    version: number;
    total_amount: string;
    currency: string;
    owner_name?: string | null;
  };
  onOpenChange: (open: boolean) => void;
}

export function ReimburseReportDialog({
  report,
  onOpenChange,
}: ReimburseReportDialogProps) {
  const reimburse = useReimburseReport();
  const { showToast } = useAtlasToast();
  const amount = formatCurrency(report.total_amount, report.currency);
  const owner = report.owner_name?.trim() || "the report owner";

  const handleConfirm = () => {
    if (reimburse.isPending) return;
    reimburse.mutate(
      { id: report.id, version: report.version },
      {
        onSuccess: (res) => {
          onOpenChange(false);
          showToast({
            variant: "success",
            title: "Marked reimbursed",
            description: `${amount} to ${owner} · reference ${res.reimbursement_reference ?? "—"}.`,
          });
        },
        onError: (err) => {
          onOpenChange(false);
          showToast(
            expenseErrorToast(err, "Couldn’t mark the report reimbursed"),
          );
        },
      },
    );
  };

  return (
    <Modal
      open
      variant="confirm"
      onOpenChange={onOpenChange}
      title="Mark as reimbursed?"
      description={`Record a reimbursement of ${amount} to ${owner} for “${report.title}”. This is a simulated payment: no real money moves. A payment reference is generated and the report becomes final — it can’t be changed afterwards.`}
      confirmLabel={
        reimburse.isPending ? "Saving…" : `Mark ${amount} reimbursed`
      }
      confirmLoading={reimburse.isPending}
      onConfirm={handleConfirm}
    />
  );
}

// ---------------------------------------------------------------------------
// Notice on the report detail
// ---------------------------------------------------------------------------

export function LifecycleNotice({
  report,
}: {
  report: Pick<
    ExpenseReport,
    | "status"
    | "currency"
    | "reimbursed_at"
    | "reimbursement_reference"
    | "reimbursement_amount"
    | "withdrawn_at"
    | "withdrawal_reason"
  >;
}) {
  if (report.status === "reimbursed") {
    return (
      <div
        role="note"
        aria-label="Reimbursement"
        data-testid="reimbursed-notice"
        className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-3"
      >
        <BadgeCheck
          className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-success)]"
          aria-hidden="true"
        />
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="atlas-text-body font-medium text-foreground">
            Reimbursed {formatDate(report.reimbursed_at)}
            {report.reimbursement_amount
              ? ` · ${formatCurrency(report.reimbursement_amount, report.currency)}`
              : ""}
          </span>
          <span className="atlas-text-caption break-all text-muted-foreground">
            Reference {report.reimbursement_reference ?? "—"} · simulated
            payment, no real money moved.
          </span>
        </div>
      </div>
    );
  }
  if (report.withdrawn_at) {
    return (
      <div
        role="note"
        aria-label="Withdrawn report"
        data-testid="withdrawn-notice"
        className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-3"
      >
        <Archive
          className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="atlas-text-body font-medium text-foreground">
            Withdrawn {formatDate(report.withdrawn_at)} — read-only
          </span>
          {report.withdrawal_reason && (
            <span className="atlas-text-caption whitespace-pre-line break-words text-muted-foreground">
              Note: {report.withdrawal_reason}
            </span>
          )}
          <span className="atlas-text-caption text-muted-foreground">
            Its review history stays on record. Select lines below to move them
            to another draft report.
          </span>
        </div>
      </div>
    );
  }
  return null;
}
