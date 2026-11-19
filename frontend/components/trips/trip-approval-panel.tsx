"use client";

import * as React from "react";
import {
  Archive,
  CheckCircle2,
  Eye,
  MessageSquare,
  RotateCcw,
  Send,
  Trash2,
} from "lucide-react";

import { AtlasButton, Modal, StatusBadge } from "@/components/atlas";
import type { StatusBadgeVariant } from "@/components/atlas";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { RowActions } from "@/components/atlas/row-actions";
import { isKnownNonOwner, isRecordOwner } from "@/lib/auth/ownership";
import { useSession } from "@/lib/auth/session-provider";
import { useDiscardTrip, useSubmitTrip } from "@/lib/query/trip-hooks";
import { useCompleteTrip, useWithdrawTrip } from "@/lib/query/lifecycle-hooks";
import { canCompleteTrip, canWithdrawTrip } from "@/lib/lifecycle/status";
import { tripApprovalView, type TripApprovalStage } from "@/lib/trips/approval";
import { isApiError } from "@/lib/types/api";
import type { TripStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * Trip approval panel — C7R.
 *
 * Tells the traveler where their trip stands and lets them submit a draft trip
 * for approval, or RESUBMIT one the approver returned for changes (with the
 * approver's comment shown verbatim). Before this, nothing in the UI could send
 * a trip for approval, so a returned trip stayed a draft indefinitely.
 *
 * UAT2 (Issue 7): the Submit / Resubmit action is offered ONLY to the trip's
 * traveler (`isRecordOwner`). Any other reader (a manager opening a teammate's
 * trip, e.g. via the planner) sees the status read-only with a
 * "Viewing <traveler>'s trip — read-only" note; the server 403s their writes.
 *
 * UAT2 defect E follow-up: with `allowDiscard` (My Trips) the traveler can
 * discard a NEVER-SUBMITTED draft (draft -> cancelled, after an in-page
 * confirmation). Never offered for a trip with approval history — a draft
 * returned for changes is fixed and resubmitted, not discarded.
 *
 * Product gap 4: a RETURNED trip can instead be WITHDRAWN by its traveler
 * (confirm dialog; cannot be undone; the review history stays) so it stops
 * holding up their other submissions. Product gap 8: an APPROVED trip whose end
 * date has passed can be marked completed by its traveler — or by finance.
 */

const BADGE: Record<TripApprovalStage, StatusBadgeVariant> = {
  draft: "neutral",
  returned: "changes-requested",
  pending: "pending",
  approved: "approved",
  rejected: "rejected",
  closed: "neutral",
};

export interface TripApprovalPanelProps {
  trip: {
    id: string;
    traveler_id: string;
    /** Traveler's display name (detail responses only) for the read-only note. */
    traveler_name?: string | null;
    status: TripStatus;
    approval_status?: string | null;
    approval_reason?: string | null;
    /** Product gaps 4 / 8 (absent on older callers → no lifecycle actions). */
    end_date?: string;
    withdrawn_at?: string | null;
  };
  /** Optional heading (e.g. the route) shown before the status. */
  title?: string;
  /** Disable Submit (e.g. while a planner selection is still being saved). */
  submitDisabled?: boolean;
  /** Extra content under the description (e.g. the trip's segments). */
  children?: React.ReactNode;
  /** Offer "Discard draft" for a never-submitted draft (My Trips only). */
  allowDiscard?: boolean;
  className?: string;
}

export function TripApprovalPanel({
  trip,
  title,
  submitDisabled = false,
  children,
  allowDiscard = false,
  className,
}: TripApprovalPanelProps) {
  const { user, role } = useSession();
  const isOwner = isRecordOwner(user, trip.traveler_id);
  const readOnlyReader = isKnownNonOwner(user, trip.traveler_id);
  const view = tripApprovalView(trip, { viewerIsOwner: isOwner });
  const submit = useSubmitTrip();
  const discard = useDiscardTrip();
  const [confirmDiscard, setConfirmDiscard] = React.useState(false);
  const withdraw = useWithdrawTrip();
  const complete = useCompleteTrip();
  const [confirmWithdraw, setConfirmWithdraw] = React.useState(false);
  const [confirmComplete, setConfirmComplete] = React.useState(false);
  const canWithdraw = isOwner && canWithdrawTrip(trip);
  const canComplete =
    (isOwner || role === "finance_admin") &&
    Boolean(trip.end_date) &&
    canCompleteTrip({ status: trip.status, end_date: trip.end_date ?? "" });
  const canDiscard =
    allowDiscard &&
    isOwner &&
    trip.status === "draft" &&
    !trip.approval_status &&
    view.stage === "draft";
  const { showToast } = useAtlasToast();
  const reason =
    view.stage === "returned" || view.stage === "rejected"
      ? trip.approval_reason?.trim() || null
      : null;

  const onSubmit = () => {
    submit.mutate(
      { id: trip.id },
      {
        onSuccess: () =>
          showToast({
            variant: "success",
            title:
              view.stage === "returned" ? "Trip resubmitted" : "Trip submitted",
            description: "Your trip is now awaiting approval.",
          }),
        onError: (err) =>
          showToast({
            variant: "error",
            title: "Couldn’t submit the trip",
            description: isApiError(err)
              ? err.message
              : "Something went wrong. Please try again.",
          }),
      },
    );
  };

  const onDiscard = () => {
    if (discard.isPending) return;
    discard.mutate(
      { id: trip.id },
      {
        onSuccess: () => {
          setConfirmDiscard(false);
          showToast({
            variant: "success",
            title: "Draft trip discarded",
            description: title
              ? `${title} was cancelled.`
              : "The draft trip was cancelled.",
          });
        },
        onError: (err) => {
          setConfirmDiscard(false);
          showToast({
            variant: "error",
            title: "Couldn’t discard the trip",
            description: isApiError(err)
              ? err.message
              : "Something went wrong. Please try again.",
          });
        },
      },
    );
  };

  const lifecycleError = (title: string) => (err: unknown) =>
    showToast({
      variant: "error",
      title,
      description: isApiError(err)
        ? err.message
        : "Something went wrong. Please try again.",
    });

  const onWithdraw = (note?: string) => {
    if (withdraw.isPending) return;
    withdraw.mutate(
      { id: trip.id, reason: note?.trim() || undefined },
      {
        onSuccess: () => {
          setConfirmWithdraw(false);
          showToast({
            variant: "success",
            title: "Trip withdrawn",
            description: title
              ? `${title} was withdrawn.`
              : "The trip was withdrawn.",
          });
        },
        onError: (err) => {
          setConfirmWithdraw(false);
          lifecycleError("Couldn’t withdraw the trip")(err);
        },
      },
    );
  };

  const onComplete = () => {
    if (complete.isPending) return;
    complete.mutate(
      { id: trip.id },
      {
        onSuccess: () => {
          setConfirmComplete(false);
          showToast({
            variant: "success",
            title: "Trip completed",
            description: title
              ? `${title} is marked completed.`
              : "The trip is marked completed.",
          });
        },
        onError: (err) => {
          setConfirmComplete(false);
          lifecycleError("Couldn’t mark the trip completed")(err);
        },
      },
    );
  };

  return (
    <section
      aria-label="Trip approval"
      data-testid="trip-approval-panel"
      className={cn(
        "flex flex-col gap-2 rounded-atlas-md border border-border bg-white p-3",
        className,
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          {title && (
            <span className="atlas-text-body truncate font-medium text-foreground">
              {title}
            </span>
          )}
          <StatusBadge variant={BADGE[view.stage]} label={view.label} />
        </div>
        {/* Follow-up: one line (nowrap); folded behind a toggle below sm. */}
        <RowActions
          label={title ? `Actions for ${title}` : "Trip actions"}
          className="gap-2"
        >
          {canWithdraw && (
            <AtlasButton
              variant="ghost"
              size="sm"
              onClick={() => setConfirmWithdraw(true)}
              disabled={withdraw.isPending}
            >
              <Archive aria-hidden="true" />
              Withdraw
            </AtlasButton>
          )}
          {canComplete && (
            <AtlasButton
              variant="secondary"
              size="sm"
              onClick={() => setConfirmComplete(true)}
              disabled={complete.isPending}
            >
              <CheckCircle2 aria-hidden="true" />
              Mark completed
            </AtlasButton>
          )}
          {canDiscard && (
            <AtlasButton
              variant="ghost"
              size="sm"
              onClick={() => setConfirmDiscard(true)}
              disabled={discard.isPending}
            >
              <Trash2 aria-hidden="true" />
              Discard draft
            </AtlasButton>
          )}
          {isOwner && view.submitLabel && (
            <AtlasButton
              variant="primary"
              size="sm"
              onClick={onSubmit}
              loading={submit.isPending}
              disabled={submitDisabled}
            >
              {view.stage === "returned" ? (
                <RotateCcw aria-hidden="true" />
              ) : (
                <Send aria-hidden="true" />
              )}
              {view.submitLabel}
            </AtlasButton>
          )}
        </RowActions>
      </div>
      {readOnlyReader && (
        <p
          role="note"
          className="flex items-start gap-1.5 atlas-text-caption font-medium text-foreground"
        >
          <Eye className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Viewing {trip.traveler_name?.trim() || "another traveler"}’s trip —
          read-only
        </p>
      )}
      <p className="atlas-text-caption text-muted-foreground">
        {view.description}
      </p>
      {children}
      {confirmDiscard && (
        <Modal
          open
          variant="destructive"
          onOpenChange={setConfirmDiscard}
          title="Discard this draft trip?"
          description={`${title ? `“${title}”` : "This draft trip"} will be cancelled. It was never submitted, so no approval is affected. This can’t be undone.`}
          confirmLabel={discard.isPending ? "Discarding…" : "Discard trip"}
          confirmLoading={discard.isPending}
          onConfirm={onDiscard}
        />
      )}
      {confirmWithdraw && (
        <Modal
          open
          variant="reason"
          requireReason={false}
          onOpenChange={setConfirmWithdraw}
          title="Withdraw this trip?"
          description={`${title ? `“${title}”` : "This trip"} was returned for changes. Withdrawing closes it for good — it can’t be resubmitted and this can’t be undone. Its review history stays on record. Add an optional note.`}
          confirmLabel={withdraw.isPending ? "Withdrawing…" : "Withdraw trip"}
          confirmLoading={withdraw.isPending}
          onConfirm={onWithdraw}
        />
      )}
      {confirmComplete && (
        <Modal
          open
          variant="confirm"
          onOpenChange={setConfirmComplete}
          title="Mark this trip completed?"
          description={`${title ? `“${title}”` : "This trip"} has ended. Completing it records that the travel happened; the trip becomes read-only.`}
          confirmLabel={complete.isPending ? "Saving…" : "Mark completed"}
          confirmLoading={complete.isPending}
          onConfirm={onComplete}
        />
      )}
      {reason && (
        <div className="flex items-start gap-2 rounded-atlas-md border border-border bg-muted p-2">
          <MessageSquare
            className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
          <div className="flex flex-col gap-0.5">
            <span className="atlas-text-caption font-medium text-muted-foreground">
              Approver’s comment
            </span>
            <span className="atlas-text-body whitespace-pre-line text-foreground">
              {reason}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
