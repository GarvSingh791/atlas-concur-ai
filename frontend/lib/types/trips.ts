import type { Citation } from "./conversations";

export type TripStatus =
  | "draft"
  | "pending_approval"
  | "approved"
  | "rejected"
  | "completed"
  | "cancelled";

/** Segment kind — mirrors database/models/trip_segment.py SegmentType. */
export type SegmentType = "flight" | "hotel" | "rail" | "car";

/**
 * Trip detail — mirrors backend travel/schemas.py TripResponse 1:1
 * (GET /trips/{id}). Money fields are Decimal on the server → JSON strings.
 */
export interface Trip {
  id: string;
  organization_id: string;
  traveler_id: string;
  origin: string;
  primary_destination: string;
  start_date: string;
  end_date: string;
  purpose: string | null;
  /** Decimal serialized as a string (e.g. "1234.50"). */
  estimated_cost: string | null;
  /** Decimal serialized as a string. */
  budget_amount: string | null;
  currency: string;
  status: TripStatus;
  recommendation_context: Record<string, unknown> | null;
  created_by: string | null;
  updated_by: string | null;
  created_at: string;
  updated_at: string;
  version: number;
  /**
   * C7R — the trip's approval as its traveler sees it (the traveler has no
   * approval.read). null/absent = never sent for approval. A trip returned for
   * changes is `status: "draft"` with `approval_status: "changes_requested"`;
   * resubmitting it (PATCH status=pending_approval) re-queues that approval.
   */
  approval_status?: string | null;
  /** Approver's comment while the trip is returned / rejected; else null. */
  approval_reason?: string | null;
  /**
   * UAT2: the traveler's display name — shown in the read-only banner when
   * someone other than the traveler (a manager) views the trip.
   */
  traveler_name?: string | null;
  /** Product gap 4: set when the traveler withdrew a returned trip (cancelled). */
  withdrawn_at?: string | null;
  withdrawal_reason?: string | null;
}

/** Lean list row — mirrors backend travel/schemas.py TripListItem (GET /trips). */
export interface TripListItem {
  id: string;
  organization_id: string;
  traveler_id: string;
  origin: string;
  primary_destination: string;
  start_date: string;
  end_date: string;
  /** UAT-2 Issue 4 — shown in the card title (absent on older servers). */
  purpose?: string | null;
  /** Decimal serialized as a string. */
  estimated_cost?: string | null;
  currency: string;
  status: TripStatus;
  version: number;
  created_at: string;
  updated_at: string;
  approval_status?: string | null;
  approval_reason?: string | null;
  /** Product gap 4 — see Trip. */
  withdrawn_at?: string | null;
}

/**
 * Trip segment — mirrors backend travel/schemas.py SegmentResponse 1:1
 * (GET /trips/{id}/segments). `amount` is Decimal on the server → JSON string.
 */
export interface TripSegment {
  id: string;
  trip_id: string;
  segment_type: SegmentType;
  supplier: string | null;
  start_datetime: string | null;
  end_datetime: string | null;
  location: string | null;
  /** Decimal serialized as a string. */
  amount: string | null;
  currency: string | null;
  in_policy: boolean | null;
  policy_notes: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface ShortlistOption {
  rank: number;
  option_id: string;
  segment_type: "flight" | "hotel";
  supplier: string;
  amount_usd: number;
  sustainability_score: number;
  cabin_class: string | null;
  duration_hours: number | null;
  is_direct: boolean | null;
  city: string | null;
  nightly_rate_usd: number | null;
  room_type: string | null;
  /** Hotel: nights of the requested stay (absent on older shortlists). */
  nights?: number | null;
  /** Hotel: nightly rate × nights (absent on older shortlists). */
  stay_total_usd?: number | null;
  in_policy: boolean;
  policy_label: "In Policy" | "Out of Policy";
  policy_reason: string | null;
  clause_refs: string[];
  requires_approver: boolean;
  explanation: string;
  /**
   * Product gap 2 (flights): process-rule notices shown at selection time —
   * PTA-02 lead time ("booked N days ahead, policy requires M"), AIR-06 lowest
   * fare, AIR-04 SVP business class. Absent on older shortlists.
   */
  policy_notices?: PolicyNotice[];
  /** Flights: the cheapest fare of the same cabin in this search (AIR-06). */
  lowest_fare_usd?: number | null;
}

/** One clause-cited process-rule notice on a planner option (product gap 2). */
export interface PolicyNotice {
  clause_ref: string;
  flag_type: string;
  message: string;
  requires_approver: string | null;
  /** True when the option breaks the clause (AIR-06), not just a notice. */
  blocking: boolean;
}

export interface PlannerIntent {
  origin: string | null;
  destination: string | null;
  departure_date: string | null;
  return_date: string | null;
  travelers: number | null;
  currency: string | null;
  // Optional trip purpose ("Client meeting") — extracted from the message text
  purpose?: string | null;
  include_flights: boolean;
  include_hotels: boolean;
  constraints: Record<string, unknown>;
}

// The slots the travel agent resolved for a turn (output.intent). The server
// merges prior turn + chips + message text, so this is the authoritative intent.
export type PlannerIntentSlots = Partial<
  Pick<
    PlannerIntent,
    | "origin"
    | "destination"
    | "departure_date"
    | "return_date"
    | "travelers"
    | "currency"
    | "purpose"
  >
>;

export interface StatusFrame {
  event: "status";
  data: {
    task_type: string;
    route: string;
    confidence: number;
    status: string;
  };
}

export interface ToolActivityFrame {
  event: "tool_activity";
  data: {
    tool_calls: unknown;
  };
}

/**
 * What a planner selection did to the conversation's draft trip (UAT-2 Issue 4:
 * one planning conversation -> one draft trip).
 */
export interface TripChange {
  action: "created" | "updated" | "unchanged";
  segment_types_added: string[];
  segment_types_replaced: string[];
  /** Set when a NEW draft was started because the earlier trip could not change. */
  previous_trip: {
    id: string;
    status: string;
    label: string;
    reason: "not_draft" | "different_trip" | string;
  } | null;
  estimated_cost: string | null;
  segment_count: number;
  /**
   * UAT2 defect E: set when ONLY the return date changed and the same draft
   * was updated in place (never a second trip).
   */
  end_date_change?: { from: string; to: string } | null;
  /** The kept hotel leg still covers the OLD stay (it was not re-selected). */
  stale_hotel?: boolean;
  /** PTA-05: the approved / under-review trip this new draft changes. */
  changes_trip_id?: string | null;
  /** PTA-05: the change raised the estimated cost > 10% — re-approval needed. */
  reapproval_required?: boolean;
}

export type AssistantResultOutput =
  | { clarify: true; question: string; intent?: PlannerIntentSlots | null }
  | {
      clarify: false;
      shortlist: ShortlistOption[];
      trip_id: string | null;
      reflection_outcome: string | null;
      degraded: boolean;
      message: string | null;
      intent?: PlannerIntentSlots | null;
      /** Selection turns only (UAT-2 Issue 4). */
      trip_change?: TripChange | null;
      /** The assistant's sentence describing the trip change. */
      trip_message?: string | null;
      /** Set when a new draft trip was started instead of changing the old one. */
      trip_notice?: string | null;
      /** The selection could not be saved (permission, stale option). */
      trip_persist_error?: string | null;
    }
  | {
      answer_type: "grounded";
      answer: string;
      asserted: string;
      critique: string | null;
    }
  | { answer_type: "silent"; reason: string; answer: null }
  | {
      /** C7-07/08: low-confidence policy answer. `answer` is the backend's
       *  provisional text (output.answer ONLY — never the unfiltered
       *  answer_structured.full_text), or null when the critic rejected the
       *  draft / no cited draft exists; the UI then shows "No confident
       *  answer" with a caution label, never blank. */
      answer_type: "needs_review";
      needs_review: true;
      answer: string | null;
      /** Citations of the answer_structured sentences that appear in `answer`
       *  (fallback when the result frame's top-level citations are empty);
       *  empty when `answer` is null. */
      citations: Citation[];
      critique: string | null;
      message: string | null;
    };

export interface ResultFrame {
  event: "result";
  data: {
    output: Record<string, unknown>;
    citations: Citation[];
  };
}

export interface NarrativeFrame {
  event: "narrative";
  data: {
    text: string;
  };
}

export interface DoneFrame {
  event: "done";
  data: {
    needs_review: boolean;
    message_id: string | null;
    sequence: number;
  };
}

export type SSEFrame =
  | StatusFrame
  | ToolActivityFrame
  | ResultFrame
  | NarrativeFrame
  | DoneFrame;
