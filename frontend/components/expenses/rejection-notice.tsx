import * as React from "react";
import {
  XCircle,
  AlertTriangle,
  Info,
  MessageSquare,
  RotateCcw,
} from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CitationChip } from "@/components/atlas";
import { cn } from "@/lib/utils";
import type { ReadinessSummary } from "@/lib/expenses/readiness";

/**
 * Rejection notice — P5-T7 (US-E5 AC2).
 *
 * BACKEND GAP (documented): the reviewer's free-text rejection reason lives in
 * `approval_steps.reason`, but the approval API is manager-gated (POST + PATCH
 * only, no GET) — there is NO employee-readable endpoint for it. So this view
 * shows what the employee CAN see: the report's own `pre_submit_flags` as the
 * "issues to resolve", each with its clause citation, plus an honest note that
 * the reviewer's written comment isn't exposed by the current API. It never
 * fabricates a reason; with no flags it shows "No reason recorded".
 *
 * C7R/P12: the report now carries `approval_reason` (the approver's comment,
 * while rejected / changes requested). When the caller passes it
 * (`reviewerReason` defined — string or null) it is shown verbatim and the gap
 * note is dropped; `undefined` means an older backend → the gap note stays.
 * `kind="changes_requested"` renders the returned-for-changes variant (the
 * report is an editable draft again).
 *
 * UAT2: `ownerView` is REQUIRED so no caller can show the owner's "edit and
 * resubmit — it goes back to your approver" copy to a reader who is not the
 * owner (a manager opening a teammate's report). A non-owner sees the outcome,
 * the reviewer's comment and the issues, with neutral wording and no call to
 * edit or resubmit.
 */

export interface RejectionNoticeProps {
  readiness: ReadinessSummary;
  /** Approver's comment; `undefined` = not exposed by this backend. */
  reviewerReason?: string | null;
  kind?: "rejected" | "changes_requested";
  /**
   * UAT2: list the flags here (default). The report detail passes `false` when
   * the pre-submit check panel below already lists them, so a returned report
   * no longer shows every issue twice.
   */
  listFlags?: boolean;
  /**
   * UAT2: true when the viewer owns the report (owner-voice "fix and resubmit"
   * copy); false for any other reader (neutral, read-only copy).
   */
  ownerView: boolean;
}

export function RejectionNotice({
  readiness,
  reviewerReason,
  kind = "rejected",
  listFlags = true,
  ownerView,
}: RejectionNoticeProps) {
  const hasFlags = readiness.flags.length > 0;
  const reasonExposed = reviewerReason !== undefined;
  const reason = reviewerReason?.trim() ? reviewerReason.trim() : null;
  const isChanges = kind === "changes_requested";

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {isChanges ? (
            <RotateCcw
              className="h-5 w-5 shrink-0 text-[var(--color-warning)]"
              aria-hidden="true"
            />
          ) : (
            <XCircle
              className="h-5 w-5 shrink-0 text-[var(--color-error)]"
              aria-hidden="true"
            />
          )}
          <span className="text-foreground">
            {isChanges ? "Changes requested" : "Report rejected"}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {reason && (
          <div className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-3">
            <MessageSquare
              className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="flex flex-col gap-0.5">
              <span className="atlas-text-caption font-medium text-muted-foreground">
                Reviewer’s comment
              </span>
              <span className="atlas-text-body whitespace-pre-line text-foreground">
                {reason}
              </span>
            </div>
          </div>
        )}
        {isChanges && ownerView && (
          <p className="atlas-text-body text-muted-foreground">
            This report is editable again. Make the changes, then submit it — it
            goes back to your approver.
          </p>
        )}
        {isChanges && !ownerView && (
          <p className="atlas-text-body text-muted-foreground">
            The approver sent this report back to its owner for changes.
          </p>
        )}
        {hasFlags && !listFlags ? (
          <p className="atlas-text-body text-muted-foreground">
            {ownerView
              ? "Fix the items in the pre-submit check below, then resubmit."
              : "The issues are listed in the pre-submit check below."}
          </p>
        ) : hasFlags ? (
          <>
            <p className="atlas-text-body text-muted-foreground">
              {ownerView
                ? "Resolve these policy issues, then this report can be re-submitted:"
                : "Policy issues on this report:"}
            </p>
            <ul className="flex flex-col gap-2">
              {readiness.flags.map((flag, i) => (
                <li
                  key={i}
                  className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-3"
                >
                  <AlertTriangle
                    className={cn(
                      "mt-0.5 h-4 w-4 shrink-0",
                      flag.severity === "issue"
                        ? "text-[var(--color-error)]"
                        : "text-[var(--color-warning)]",
                    )}
                    aria-hidden="true"
                  />
                  <div className="flex flex-col gap-1">
                    <span className="atlas-text-body text-foreground">
                      {flag.message}
                    </span>
                    {flag.clauseRef && (
                      <span className="mt-0.5">
                        <CitationChip clauseRef={flag.clauseRef} />
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          </>
        ) : reason ? null : (
          <p className="atlas-text-body text-muted-foreground">
            No reason was recorded on this report.
          </p>
        )}

        {/* Honest gap note (per US-E5 reconciliation) — only when this backend
            does not expose the reviewer's comment at all. */}
        {!reasonExposed && (
          <p className="flex items-start gap-2 atlas-text-caption text-muted-foreground">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            The reviewer’s written comment isn’t available here — the current
            API doesn’t expose approval decision notes to employees. The policy
            flags above are the issues {ownerView ? "your" : "the"} reviewer
            would have seen.
          </p>
        )}
      </CardContent>
    </Card>
  );
}
