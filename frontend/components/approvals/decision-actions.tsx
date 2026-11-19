"use client";

import * as React from "react";
import { Check, MessageSquare, X } from "lucide-react";

import { AtlasButton, Modal, useAtlasToast } from "@/components/atlas";
import { useDecideApproval } from "@/lib/query/approval-hooks";
import { API_ERROR_CODES, isApiError } from "@/lib/types/api";
import type { ApprovalDetail } from "@/lib/types/approvals";

/**
 * Decision actions — Approve (immediate) / Request changes / Reject.
 *
 * - reject + request_changes require a reason (client-enforced via the Modal's
 *   required textarea; the server ALSO enforces it).
 * - Every decision carries the current `version` as If-Match for optimistic
 *   concurrency; a 409 surfaces as a toast (no silent overwrite) and the panel
 *   reloads the latest via the mutation's cache invalidation.
 * - Decidable on pending OR auto_approved (reverse-within-policy, US-M2 AC2).
 * - Separation of duties (C7-01/02/03): the submitter can never decide their
 *   own item. The server enforces it (PATCH → 403); the UI hides the actions
 *   and explains why. A 403 / 400 INVALID_SUBJECT_STATE from the server shows
 *   the envelope's message rather than a generic failure.
 * - Approver grade (follow-up, grades): when the server says the viewer's grade
 *   is below the one the flags require (`grade_requirement.viewer_can_decide`
 *   false), the buttons are disabled and the server's explanation is shown; the
 *   server refuses the decision anyway (403 INSUFFICIENT_APPROVER_GRADE).
 */

type ReasonDecision = "reject" | "request_changes";

/** QA round: past-tense outcome of a closed approval, for the "can't be changed" note. */
const CLOSED_OUTCOME: Record<string, string> = {
  approved: "approved",
  rejected: "rejected",
  changes_requested: "returned for changes",
};

/** Why a non-decidable approval can't be decided, in plain English. */
export function closedApprovalNote(approval: ApprovalDetail): string {
  if (
    approval.subject_withdrawn === true ||
    approval.display_status === "withdrawn"
  ) {
    const noun = approval.subject_type === "trip" ? "trip" : "report";
    return `This ${noun} was withdrawn by its owner; the approval is closed.`;
  }
  const outcome = CLOSED_OUTCOME[approval.current_status];
  return outcome
    ? `This approval was ${outcome} and can’t be changed.`
    : "This approval is closed and can’t be changed.";
}

export function DecisionActions({
  approval,
  canDecide,
  currentUserId = null,
}: {
  approval: ApprovalDetail;
  canDecide: boolean;
  /** Signed-in user's id — used to block deciding one's own submission. */
  currentUserId?: string | null;
}) {
  const { showToast } = useAtlasToast();
  const decide = useDecideApproval();
  const [modal, setModal] = React.useState<ReasonDecision | null>(null);

  const decidable =
    approval.current_status === "pending" ||
    approval.current_status === "auto_approved";
  const isReverse = approval.current_status === "auto_approved";

  async function run(
    decision: "approve" | ReasonDecision,
    reason?: string,
  ): Promise<void> {
    try {
      await decide.mutateAsync({
        id: approval.id,
        decision,
        reason: reason ?? null,
        version: approval.version,
      });
      showToast({
        variant: "success",
        title:
          decision === "approve"
            ? "Approved"
            : decision === "reject"
              ? "Rejected"
              : "Changes requested",
      });
    } catch (err) {
      if (isApiError(err) && err.code === API_ERROR_CODES.CONFLICT) {
        showToast({
          variant: "error",
          title: "This approval changed",
          description:
            "Someone updated it first — reloading the latest version.",
        });
      } else if (
        isApiError(err) &&
        err.code === API_ERROR_CODES.INSUFFICIENT_APPROVER_GRADE
      ) {
        showToast({
          variant: "error",
          title: "A more senior approver must decide this",
          description: err.message,
        });
      } else if (isApiError(err) && err.code === API_ERROR_CODES.FORBIDDEN) {
        showToast({
          variant: "error",
          title: "You can’t decide this approval",
          description: err.message,
        });
      } else if (
        isApiError(err) &&
        err.code === API_ERROR_CODES.INVALID_SUBJECT_STATE
      ) {
        showToast({
          variant: "error",
          title: "No longer awaiting a decision",
          description: err.message,
        });
      } else {
        showToast({
          variant: "error",
          title: "Decision failed",
          description: isApiError(err) ? err.message : "Please try again.",
        });
      }
    } finally {
      setModal(null);
    }
  }

  if (!canDecide) {
    return (
      <p className="atlas-text-caption text-muted-foreground">
        You don’t have permission to decide on this approval.
      </p>
    );
  }

  if (currentUserId && approval.submitter_id === currentUserId) {
    return (
      <p className="atlas-text-caption text-muted-foreground">
        You submitted this item, so you can’t approve, reject or request changes
        on it. Another approver will decide.
      </p>
    );
  }

  if (!decidable) {
    return (
      <p className="atlas-text-caption text-muted-foreground">
        {closedApprovalNote(approval)}
      </p>
    );
  }

  const busy = decide.isPending;
  // Follow-up (grades): the server says this viewer's grade is below the one the
  // flags require — the buttons stay visible but disabled, with the reason.
  const gradeBlocked = approval.grade_requirement?.viewer_can_decide === false;
  const blocked = busy || gradeBlocked;

  return (
    <div className="flex flex-col gap-[var(--space-3)]">
      {gradeBlocked && approval.grade_requirement && (
        <p
          role="note"
          data-testid="grade-blocked-note"
          className="atlas-text-caption text-[var(--color-warning)]"
        >
          {approval.grade_requirement.message}
        </p>
      )}
      {isReverse && (
        <p className="atlas-text-caption text-muted-foreground">
          This item was auto-approved. A decision here records a human override
          (reverse within policy).
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        <AtlasButton
          variant="primary"
          size="md"
          onClick={() => run("approve")}
          disabled={blocked}
        >
          <Check className="h-4 w-4" aria-hidden="true" /> Approve
        </AtlasButton>
        <AtlasButton
          variant="secondary"
          size="md"
          onClick={() => setModal("request_changes")}
          disabled={blocked}
        >
          <MessageSquare className="h-4 w-4" aria-hidden="true" /> Request
          changes
        </AtlasButton>
        <AtlasButton
          variant="destructive"
          size="md"
          onClick={() => setModal("reject")}
          disabled={blocked}
        >
          <X className="h-4 w-4" aria-hidden="true" /> Reject
        </AtlasButton>
      </div>

      <Modal
        open={modal === "reject"}
        onOpenChange={(o) => !o && setModal(null)}
        variant="reason"
        requireReason
        title="Reject approval"
        description="Provide a reason. It is recorded and shown to the submitter."
        confirmLabel="Reject"
        onConfirm={(reason) => run("reject", reason)}
        onCancel={() => setModal(null)}
      />
      <Modal
        open={modal === "request_changes"}
        onOpenChange={(o) => !o && setModal(null)}
        variant="reason"
        requireReason
        title="Request changes"
        description="Tell the submitter what needs to change. It is recorded."
        confirmLabel="Request changes"
        onConfirm={(reason) => run("request_changes", reason)}
        onCancel={() => setModal(null)}
      />
    </div>
  );
}
