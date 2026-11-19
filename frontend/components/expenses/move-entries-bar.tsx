"use client";

import * as React from "react";
import { ArrowRightLeft } from "lucide-react";

import { AtlasButton } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { useExpenseReports, useMoveEntries } from "@/lib/query/expense-hooks";
import { expenseErrorToast } from "@/lib/expenses/mutation-errors";
import type { ExpenseReport } from "@/lib/types";
import { CreateReportDialog } from "./report-dialogs";

/**
 * Move selected entries to another draft report — C7R UAT report assembly.
 *
 * Rendered by the report detail only while entries are selected on a DRAFT
 * report the viewer owns. Targets are the viewer's OTHER draft reports (the
 * server re-checks ownership + draft state of both reports); "New report…"
 * creates an empty draft and moves the selection into it. The source report's
 * version goes in If-Match, so a stale selection is a 409, never a silent move.
 *
 * Segregation of duties (C7R review): a report a reviewer RETURNED (it is a
 * draft that carries an approval status — changes requested, or rejected and
 * reopened) keeps its entries. They are fixed on it and the report is
 * resubmitted to the same reviewer; moving them to a fresh report (which the
 * agent could auto-approve) is refused by the server (409
 * REPORT_UNDER_REVIEW). Here the Move control is disabled with the reason.
 * Moving entries INTO a returned draft is allowed (it goes back to the same
 * reviewer on resubmission — e.g. "add the missing taxi receipt"), so returned
 * drafts ARE offered as targets, labelled as returned.
 */

export const RETURNED_REPORT_MOVE_REASON =
  "A reviewer returned this report, so its entries stay on it. Fix them here and resubmit for review.";

/** A draft that went through review and came back (server-enforced too). */
export function isReturnedReport(
  r: Pick<ExpenseReport, "status" | "approval_status">,
): boolean {
  return r.status === "draft" && Boolean(r.approval_status);
}

const NEW_REPORT = "__new__";

export interface MoveEntriesBarProps {
  report: Pick<
    ExpenseReport,
    "id" | "version" | "owner_id" | "status" | "approval_status"
  > & { withdrawn_at?: string | null };
  selectedIds: string[];
  onMoved: () => void;
}

export function MoveEntriesBar({
  report,
  selectedIds,
  onMoved,
}: MoveEntriesBarProps) {
  const [target, setTarget] = React.useState("");
  const [creating, setCreating] = React.useState(false);
  const drafts = useExpenseReports({ status: "draft", size: 100 });
  const move = useMoveEntries();
  const { showToast } = useAtlasToast();

  // Product gap 4: a WITHDRAWN report releases its lines (server-allowed).
  const returned = isReturnedReport(report) && !report.withdrawn_at;
  const targets = (drafts.data?.items ?? []).filter(
    (r) => r.id !== report.id && r.owner_id === report.owner_id,
  );
  const count = selectedIds.length;
  const noun = `entr${count === 1 ? "y" : "ies"}`;

  const doMove = (targetId: string, targetTitle: string) => {
    move.mutate(
      {
        sourceId: report.id,
        sourceVersion: report.version,
        targetId,
        entryIds: selectedIds,
      },
      {
        onSuccess: () => {
          showToast({
            variant: "success",
            title: `Moved ${count} ${noun}`,
            description: `Now on “${targetTitle}”. Both reports were re-checked.`,
          });
          setTarget("");
          onMoved();
        },
        onError: (err) =>
          showToast(expenseErrorToast(err, "Couldn’t move the entries")),
      },
    );
  };

  const handleMove = () => {
    if (target === NEW_REPORT) {
      setCreating(true);
      return;
    }
    const t = targets.find((r) => r.id === target);
    if (t) doMove(t.id, t.title);
  };

  return (
    <div
      className="flex flex-col gap-2 rounded-atlas-md border border-border bg-muted p-3 sm:flex-row sm:flex-wrap sm:items-center"
      role="region"
      aria-label="Move selected entries"
    >
      <span className="atlas-text-body text-foreground">
        {count} {noun} selected
      </span>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2 sm:justify-end">
        <label htmlFor="move-target" className="sr-only">
          Move to report
        </label>
        <select
          id="move-target"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          disabled={returned}
          aria-describedby={returned ? "move-locked-reason" : undefined}
          className="h-9 min-w-0 max-w-full flex-1 rounded-md border border-input bg-background px-2 text-sm text-foreground sm:max-w-xs"
        >
          <option value="">Move to…</option>
          {targets.map((r) => (
            <option key={r.id} value={r.id}>
              {isReturnedReport(r)
                ? `${r.title} (returned for changes)`
                : r.title}
            </option>
          ))}
          <option value={NEW_REPORT}>New report…</option>
        </select>
        <AtlasButton
          variant="primary"
          size="sm"
          onClick={handleMove}
          disabled={returned || !target || move.isPending}
          disabledReason={returned ? RETURNED_REPORT_MOVE_REASON : undefined}
          loading={move.isPending}
          aria-describedby={returned ? "move-locked-reason" : undefined}
        >
          <ArrowRightLeft className="h-4 w-4" aria-hidden="true" />
          Move
        </AtlasButton>
      </div>
      {returned && (
        <p
          id="move-locked-reason"
          className="atlas-text-caption text-muted-foreground sm:basis-full"
        >
          {RETURNED_REPORT_MOVE_REASON}
        </p>
      )}

      {creating && (
        <CreateReportDialog
          onOpenChange={setCreating}
          description={`Creates an empty draft, then moves the ${count} selected ${noun} into it.`}
          onCreated={(created) => doMove(created.id, created.title)}
        />
      )}
    </div>
  );
}
