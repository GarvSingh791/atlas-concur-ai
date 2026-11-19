"use client";

import * as React from "react";
import {
  Archive,
  ArrowLeft,
  BadgeCheck,
  Eye,
  Paperclip,
  Pencil,
  Route,
  Trash2,
} from "lucide-react";

import { AtlasButton, LoadingState, ErrorState } from "@/components/atlas";
import { ReportStatusBadge } from "./report-status-badge";
import { EntriesTable, entryLabel } from "./entries-table";
import { AttachReceiptDialog } from "./attach-receipt-dialog";
import { AddMileageDialog } from "./add-mileage-dialog";
import { DeleteEntryDialog, DeleteReportDialog } from "./delete-dialogs";
import { EntryEditor } from "./entry-editor";
import { MoveEntriesBar } from "./move-entries-bar";
import { RenameReportDialog } from "./report-dialogs";
import { ReadinessPanel } from "./readiness-panel";
import { SubmitBar } from "./submit-bar";
import { RejectionNotice } from "./rejection-notice";
import {
  LifecycleNotice,
  ReimburseReportDialog,
  WithdrawReportDialog,
} from "./lifecycle-dialogs";
import { useSession } from "@/lib/auth/session-provider";
import {
  canReimburse,
  canWithdrawReport,
  displayReportStatus,
  isWithdrawnReport,
} from "@/lib/lifecycle/status";
import { isKnownNonOwner, isRecordOwner } from "@/lib/auth/ownership";
import { useExpenseReport, useReportEntries } from "@/lib/query/expense-hooks";
import {
  normalizePreSubmitFlags,
  type ReadinessFlag,
} from "@/lib/expenses/readiness";
import { formatCurrency } from "@/lib/expenses/format";
import {
  entryPolicyVerdicts,
  type EntryPolicyVerdict,
} from "@/lib/approvals/entry-policy";
import type { ExpenseEntry } from "@/lib/types";

/**
 * Report detail — P5-T7.
 *
 * Header (title · server-derived total · status) + entries table with per-entry
 * lineage + pre-submit readiness + submit gate (draft) + rejection notice
 * (rejected). All values come from the server; the screen computes no totals
 * and re-evaluates no policy.
 *
 * C7R UAT: a DRAFT report viewed by its OWNER is editable — each entry gets a
 * real "Edit" action (inline EntryEditor: fields + justification), flags offer
 * "Add justification", entries can be selected and moved to another draft, and
 * the report can be renamed. Every write re-runs the server's pre-submit check
 * and the detail refetches it. Anything else (submitted / approved / rejected,
 * or a manager / finance viewer) stays read-only; the server enforces the same.
 *
 * UAT2: an owner's draft also offers "Attach receipt" (upload, or pick an
 * unattached receipt) and "Delete" per entry (in-page confirm; offered only when
 * the server marks the entry deletable), and a never-submitted draft can be
 * deleted. A returned report lists its issues once (in the pre-submit check).
 *
 * UAT2 (Issue 7): EVERY owner action — Submit / Resubmit, Edit, Justify, Move,
 * Rename, Delete, Attach receipt — keys on the one `isRecordOwner` predicate.
 * A reader who is not the owner (a manager via the team read scope) gets a
 * "Viewing <owner>'s report — read-only" banner, neutral notice copy and no
 * controls; the server rejects their writes regardless (403).
 */

export interface ReportDetailProps {
  reportId: string;
  onBack: () => void;
}

function BackButton({ onBack }: { onBack: () => void }) {
  return (
    <AtlasButton variant="ghost" size="sm" onClick={onBack}>
      <ArrowLeft className="h-4 w-4" aria-hidden="true" />
      All reports
    </AtlasButton>
  );
}

/** Server flags → per-entry verdicts (by entry id), for the POLICY column. */
function verdictsFromFlags(
  entries: ExpenseEntry[],
  flags: ReadinessFlag[],
): Map<string, EntryPolicyVerdict> {
  return entryPolicyVerdicts(
    entries,
    flags.map((f) => ({
      entry_ref: f.entryRef,
      entry_id: f.entryId ?? null,
      severity: f.severity === "issue" ? "blocking" : "advisory",
    })),
  );
}

interface EditTarget {
  entryId: string;
  justify: boolean;
}

export function ReportDetail({ reportId, onBack }: ReportDetailProps) {
  const reportQuery = useExpenseReport(reportId);
  const entriesQuery = useReportEntries(reportId);
  const session = useSession();
  const { user } = session;
  // Fail closed: no permission check available → no finance action offered.
  const mayReimburse =
    typeof session.hasPermission === "function" &&
    session.hasPermission("expense.reimburse.org");
  const [withdrawing, setWithdrawing] = React.useState(false);
  const [reimbursing, setReimbursing] = React.useState(false);
  const [editing, setEditing] = React.useState<EditTarget | null>(null);
  const [selected, setSelected] = React.useState<Set<string>>(new Set());
  const [renaming, setRenaming] = React.useState(false);
  const [attachFor, setAttachFor] = React.useState<string | null>(null);
  const [deleteFor, setDeleteFor] = React.useState<string | null>(null);
  const [deletingReport, setDeletingReport] = React.useState(false);
  const [addingMileage, setAddingMileage] = React.useState(false);
  const editorRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (editing) editorRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [editing]);

  if (reportQuery.isLoading) {
    return (
      <div className="flex flex-col gap-4">
        <BackButton onBack={onBack} />
        <LoadingState variant="card" rows={1} />
        <LoadingState variant="table" rows={4} />
      </div>
    );
  }

  if (reportQuery.isError || !reportQuery.data) {
    return (
      <div className="flex flex-col gap-4">
        <BackButton onBack={onBack} />
        <ErrorState
          heading="Couldn’t load this report"
          subtext="Something went wrong fetching the report."
          onRetry={() => reportQuery.refetch()}
        />
      </div>
    );
  }

  const report = reportQuery.data;
  const readiness = normalizePreSubmitFlags(report.pre_submit_flags);

  const entriesState: "default" | "loading" | "empty" | "error" =
    entriesQuery.isLoading
      ? "loading"
      : entriesQuery.isError
        ? "error"
        : (entriesQuery.data?.length ?? 0) === 0
          ? "empty"
          : "default";

  // C7R/P12: a request-changes decision returns the report to `draft`; its
  // approval (owner-visible approval_status) says why. The approver's comment
  // is passed only when the report actually has an approval (otherwise
  // undefined → the notice keeps its "not exposed" note).
  // Product gap 4: a withdrawn report is closed — no notices, no edits; its
  // lines may only be moved out (released) by its owner.
  const withdrawn = isWithdrawnReport(report);
  const returnedForChanges =
    report.status === "draft" &&
    !withdrawn &&
    report.approval_status === "changes_requested";
  const reviewerReason = report.approval_status
    ? (report.approval_reason ?? null)
    : undefined;

  // QA round: "pre-submit" advice ("will require approver sign-off after
  // submission") is meaningless once the report is decided — approved and
  // reimbursed reports no longer show the panel.
  const showReadiness =
    !withdrawn &&
    report.status !== "rejected" &&
    report.status !== "approved" &&
    report.status !== "reimbursed" &&
    (report.status === "draft" || readiness.flags.length > 0);

  // Owner + draft → editable (the server enforces both on every write).
  const isOwner = isRecordOwner(user, report.owner_id);
  // Banner only once the session is known — an owner never sees a flash of it.
  const readOnlyReader = isKnownNonOwner(user, report.owner_id);
  const canEdit = report.status === "draft" && isOwner && !withdrawn;
  // Lines of a withdrawn report are released: its owner may move them out.
  const canRelease = isOwner && withdrawn;
  const canSelect = canEdit || canRelease;
  const showWithdraw = isOwner && canWithdrawReport(report);
  const showReimburse = canReimburse(report, user, mayReimburse);
  const entries = entriesQuery.data ?? [];
  const editingEntry = canEdit
    ? entries.find((e) => e.id === editing?.entryId)
    : undefined;
  const selectedIds = entries
    .filter((e) => selected.has(e.id))
    .map((e) => e.id);
  const policyVerdicts =
    readiness.flags.length > 0
      ? verdictsFromFlags(entries, readiness.flags)
      : undefined;
  const entryById = new Map(entries.map((e) => [e.id, e]));
  const attachEntry = canEdit ? entryById.get(attachFor ?? "") : undefined;
  const deleteEntry = canEdit ? entryById.get(deleteFor ?? "") : undefined;
  // Server-computed (UAT2): a reviewed line whose return froze no snapshot is
  // not deletable — never offer a delete the server will refuse.
  const canDeleteEntry = (id: string) => entryById.get(id)?.deletable === true;
  const canDeleteReport = canEdit && !report.approval_status;

  return (
    <div className="flex flex-col gap-5">
      <BackButton onBack={onBack} />

      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 flex-col gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <h1 className="atlas-text-display break-words text-foreground">
              {report.title}
            </h1>
            {canEdit && (
              <AtlasButton
                variant="ghost"
                size="sm"
                onClick={() => setRenaming(true)}
                aria-label="Rename report"
              >
                <Pencil className="h-4 w-4" aria-hidden="true" />
                Rename
              </AtlasButton>
            )}
            {showWithdraw && (
              <AtlasButton
                variant="ghost"
                size="sm"
                onClick={() => setWithdrawing(true)}
                aria-label="Withdraw report"
              >
                <Archive className="h-4 w-4" aria-hidden="true" />
                Withdraw
              </AtlasButton>
            )}
            {showReimburse && (
              <AtlasButton
                variant="secondary"
                size="sm"
                onClick={() => setReimbursing(true)}
                aria-label="Mark reimbursed"
              >
                <BadgeCheck className="h-4 w-4" aria-hidden="true" />
                Mark reimbursed
              </AtlasButton>
            )}
            {canDeleteReport && (
              <AtlasButton
                variant="ghost"
                size="sm"
                onClick={() => setDeletingReport(true)}
                aria-label="Delete report"
              >
                <Trash2 className="h-4 w-4" aria-hidden="true" />
                Delete
              </AtlasButton>
            )}
          </div>
          <div className="flex items-center gap-3">
            <span className="flex flex-col items-end">
              <span className="font-mono text-lg text-foreground">
                {formatCurrency(report.total_amount, report.currency)}
              </span>
              {entries.some((e) => e.amount_conversion) && (
                // Product gap 8: the total is in the report currency; foreign
                // entries were converted server-side (shown per line).
                <span
                  className="atlas-text-caption text-muted-foreground"
                  data-testid="report-total-converted-note"
                >
                  Total in {report.currency} · includes converted
                  foreign-currency entries
                </span>
              )}
            </span>
            <ReportStatusBadge status={displayReportStatus(report)} />
          </div>
        </div>
      </div>

      {readOnlyReader && (
        <div
          role="note"
          aria-label="Read-only report"
          className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-3"
        >
          <Eye
            className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="atlas-text-body break-words font-medium text-foreground">
              Viewing {report.owner_name?.trim() || "another employee"}’s report
              — read-only
            </span>
            <span className="atlas-text-caption text-muted-foreground">
              Only the report’s owner can edit, submit or delete it.
            </span>
          </div>
        </div>
      )}

      <LifecycleNotice report={report} />

      {report.status === "rejected" && (
        <RejectionNotice
          readiness={readiness}
          reviewerReason={reviewerReason}
          ownerView={isOwner}
        />
      )}
      {returnedForChanges && (
        <RejectionNotice
          kind="changes_requested"
          readiness={readiness}
          reviewerReason={reviewerReason}
          ownerView={isOwner}
          // The pre-submit check below lists the same issues (UAT2: once).
          listFlags={!showReadiness}
        />
      )}

      {showReadiness && (
        <ReadinessPanel
          readiness={readiness}
          onEditEntry={
            canEdit
              ? (entryId, { justify }) => setEditing({ entryId, justify })
              : undefined
          }
          onAttachReceipt={canEdit ? (id) => setAttachFor(id) : undefined}
          onDeleteEntry={canEdit ? (id) => setDeleteFor(id) : undefined}
          canDeleteEntry={canDeleteEntry}
        />
      )}

      {/* Entries */}
      <div className="flex min-w-0 flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="atlas-text-title text-foreground">Entries</h2>
          {canEdit && (
            <AtlasButton
              variant="secondary"
              size="sm"
              onClick={() => setAddingMileage(true)}
              data-testid="add-mileage"
            >
              <Route className="h-4 w-4" aria-hidden="true" />
              Add mileage
            </AtlasButton>
          )}
        </div>
        {canSelect && selectedIds.length > 0 && (
          <MoveEntriesBar
            report={report}
            selectedIds={selectedIds}
            onMoved={() => {
              setSelected(new Set());
              setEditing(null);
            }}
          />
        )}
        <EntriesTable
          entries={entries}
          state={entriesState}
          onRetry={() => entriesQuery.refetch()}
          policyVerdicts={policyVerdicts}
          onEditEntry={
            canEdit
              ? (e) => setEditing({ entryId: e.id, justify: false })
              : undefined
          }
          selection={
            canSelect ? { selected, onChange: setSelected } : undefined
          }
          rowActions={
            canEdit
              ? (e) => (
                  <>
                    <AtlasButton
                      variant="ghost"
                      size="sm"
                      onClick={() => setAttachFor(e.id)}
                      aria-label={`Attach receipt to ${entryLabel(e)}`}
                    >
                      <Paperclip className="h-4 w-4" aria-hidden="true" />
                    </AtlasButton>
                    {e.deletable && (
                      <AtlasButton
                        variant="ghost"
                        size="sm"
                        onClick={() => setDeleteFor(e.id)}
                        aria-label={`Delete ${entryLabel(e)}`}
                      >
                        <Trash2 className="h-4 w-4" aria-hidden="true" />
                      </AtlasButton>
                    )}
                  </>
                )
              : undefined
          }
          emptySubtext={
            canEdit
              ? "No entries yet. Open another draft report (such as your Receipt Inbox), select entries and move them here."
              : undefined
          }
        />
        {editingEntry && (
          <div ref={editorRef}>
            <EntryEditor
              key={`${editingEntry.id}:${editing?.justify ? "j" : "e"}`}
              entry={editingEntry}
              report={report}
              flags={readiness.flags.filter(
                (f) => f.entryId === editingEntry.id,
              )}
              focusJustification={editing?.justify ?? false}
              onClose={() => setEditing(null)}
              onAttachReceipt={() => {
                setEditing(null);
                setAttachFor(editingEntry.id);
              }}
            />
          </div>
        )}
      </div>

      {canEdit && (
        <SubmitBar
          report={report}
          readiness={readiness}
          entryCount={entriesQuery.data ? entries.length : undefined}
        />
      )}

      {showWithdraw && withdrawing && (
        <WithdrawReportDialog report={report} onOpenChange={setWithdrawing} />
      )}
      {showReimburse && reimbursing && (
        <ReimburseReportDialog report={report} onOpenChange={setReimbursing} />
      )}
      {canEdit && addingMileage && (
        <AddMileageDialog report={report} onOpenChange={setAddingMileage} />
      )}
      {canEdit && renaming && (
        <RenameReportDialog report={report} onOpenChange={setRenaming} />
      )}
      {attachEntry && (
        <AttachReceiptDialog
          entry={attachEntry}
          report={report}
          reportEntryIds={entries.map((e) => e.id)}
          onOpenChange={(open) => !open && setAttachFor(null)}
        />
      )}
      {deleteEntry && (
        <DeleteEntryDialog
          entry={deleteEntry}
          report={report}
          onOpenChange={(open) => !open && setDeleteFor(null)}
          onDeleted={() => {
            setSelected(new Set());
            if (editing?.entryId === deleteEntry.id) setEditing(null);
          }}
        />
      )}
      {canDeleteReport && deletingReport && (
        <DeleteReportDialog
          report={report}
          entryCount={entries.length}
          onOpenChange={setDeletingReport}
          onDeleted={onBack}
        />
      )}
    </div>
  );
}
