/**
 * Trip approval state as the traveler sees it (C7R).
 *
 * A trip the approver returned with "request changes" goes back to `draft`
 * (backend approval/service.py) and carries `approval_status:
 * "changes_requested"` + the approver's `approval_reason`. Only a draft trip can
 * be (re)submitted; resubmission re-queues the same approval for a fresh,
 * independent human decision.
 *
 * UAT2 (Issue 7): only the traveler may submit or resubmit. For any other
 * viewer (`viewerIsOwner: false` — a manager reading a teammate's trip) the
 * view never offers a submit action and uses neutral, not traveler-voice, copy.
 */
import type { TripStatus } from "@/lib/types";

export type TripApprovalStage =
  | "draft"
  | "returned"
  | "pending"
  | "approved"
  | "rejected"
  | "closed";

export interface TripApprovalView {
  stage: TripApprovalStage;
  label: string;
  description: string;
  /** Label of the submit action, or null when the trip cannot be submitted. */
  submitLabel: string | null;
}

export function tripApprovalView(
  trip: {
    status: TripStatus;
    approval_status?: string | null;
    /** Product gap 4: set when the traveler withdrew a returned trip. */
    withdrawn_at?: string | null;
  },
  opts: { viewerIsOwner?: boolean } = {},
): TripApprovalView {
  const view = travelerView(trip);
  if (opts.viewerIsOwner !== false) return view;
  // A reader who is not the traveler: same stage + label, no action.
  const description =
    view.stage === "returned"
      ? "The approver sent this trip back to its traveler for changes."
      : view.stage === "draft"
        ? "This trip hasn't been sent for approval yet."
        : view.description;
  return { ...view, description, submitLabel: null };
}

function travelerView(trip: {
  status: TripStatus;
  approval_status?: string | null;
  withdrawn_at?: string | null;
}): TripApprovalView {
  if (trip.status === "cancelled" && trip.withdrawn_at) {
    return {
      stage: "closed",
      label: "Withdrawn",
      description:
        "This trip was withdrawn after it was returned. Its review history stays on record; it can't be changed.",
      submitLabel: null,
    };
  }
  switch (trip.status) {
    case "draft":
      if (trip.approval_status === "changes_requested") {
        return {
          stage: "returned",
          label: "Changes requested",
          description:
            "Your approver sent this trip back. Make the changes, then resubmit it for approval.",
          submitLabel: "Resubmit for approval",
        };
      }
      return {
        stage: "draft",
        label: "Draft",
        description:
          "This trip is saved but not yet sent for approval. Submit it when you're ready.",
        submitLabel: "Submit for approval",
      };
    case "pending_approval":
      return {
        stage: "pending",
        label: "Awaiting approval",
        description:
          "This trip has been submitted and is waiting for approval.",
        submitLabel: null,
      };
    case "approved":
    case "completed":
      return {
        stage: "approved",
        label: trip.status === "completed" ? "Completed" : "Approved",
        description:
          trip.status === "completed"
            ? "This trip is complete. It's read-only now."
            : "This trip is approved.",
        submitLabel: null,
      };
    case "rejected":
      return {
        stage: "rejected",
        label: "Rejected",
        description: "This trip was rejected.",
        submitLabel: null,
      };
    default:
      return {
        stage: "closed",
        label: "Cancelled",
        description: "This trip was cancelled.",
        submitLabel: null,
      };
  }
}
