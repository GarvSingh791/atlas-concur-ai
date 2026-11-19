"use client";

import * as React from "react";
import { Send } from "lucide-react";

import { AtlasButton, Modal } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { useSubmitReport } from "@/lib/query/expense-hooks";
import { computeSubmitGate } from "@/lib/expenses/submit-gate";
import { isApiError } from "@/lib/types/api";
import type { ExpenseReport } from "@/lib/types";
import type { ReadinessSummary } from "@/lib/expenses/readiness";

/**
 * Submit bar — P5-T7 (US-E4 AC3).
 *
 * Renders only for DRAFT reports. The gate is computed from readiness severity
 * (presentation only): green → submit; amber → warning-confirmation modal first;
 * red → disabled with a tooltip. Carries the report `version` as an If-Match
 * header; a 409 CONFLICT surfaces as an error toast (no silent overwrite).
 */

export interface SubmitBarProps {
  report: ExpenseReport;
  readiness: ReadinessSummary;
  /**
   * UAT2 (M12): number of entries on the report (undefined = unknown, e.g.
   * still loading). An empty never-reviewed report can't be submitted (the
   * server refuses it too, REPORT_EMPTY); a returned report is exempt.
   */
  entryCount?: number;
}

const EMPTY_GATE = {
  color: "red",
  canSubmit: false,
  requiresConfirm: false,
  tooltip: "Add at least one expense before submitting.",
} as const;

export function SubmitBar({ report, readiness, entryCount }: SubmitBarProps) {
  const empty = entryCount === 0 && !report.approval_status;
  const gate = empty ? EMPTY_GATE : computeSubmitGate(readiness.level);
  const submit = useSubmitReport();
  const { showToast } = useAtlasToast();
  const [confirmOpen, setConfirmOpen] = React.useState(false);

  if (report.status !== "draft") return null;

  const doSubmit = () => {
    setConfirmOpen(false);
    submit.mutate(
      { id: report.id, version: report.version },
      {
        onSuccess: () => {
          showToast({
            variant: "success",
            title: "Report submitted",
            description: `“${report.title}” is now awaiting approval.`,
          });
        },
        onError: (err) => {
          if (isApiError(err) && err.code === "CONFLICT") {
            showToast({
              variant: "error",
              title: "This report changed",
              description:
                "It was updated since you opened it. Refresh and try again.",
            });
            return;
          }
          showToast({
            variant: "error",
            title: "Couldn’t submit",
            description: isApiError(err)
              ? err.message
              : "Something went wrong. Please try again.",
          });
        },
      },
    );
  };

  const handleClick = () => {
    if (gate.requiresConfirm) {
      setConfirmOpen(true);
    } else {
      doSubmit();
    }
  };

  return (
    <div className="flex flex-wrap items-center justify-end gap-3">
      {empty && (
        <span className="atlas-text-caption text-muted-foreground">
          Add at least one expense
        </span>
      )}
      {gate.color === "amber" && (
        <span className="atlas-text-caption text-[var(--color-warning)]">
          Submitting with {readiness.reviewCount} item
          {readiness.reviewCount === 1 ? "" : "s"} to review
        </span>
      )}

      <AtlasButton
        variant="primary"
        size="md"
        onClick={handleClick}
        disabled={!gate.canSubmit}
        loading={submit.isPending}
        disabledReason={gate.tooltip ?? undefined}
      >
        <Send className="h-4 w-4" aria-hidden="true" />
        Submit report
      </AtlasButton>

      <Modal
        variant="confirm"
        open={confirmOpen}
        onOpenChange={setConfirmOpen}
        title="Submit with warnings?"
        description={`This report has ${readiness.reviewCount} item${
          readiness.reviewCount === 1 ? "" : "s"
        } flagged for review. You can still submit — your approver will see the flags.`}
        confirmLabel="Submit anyway"
        onConfirm={doSubmit}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}
