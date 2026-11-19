import type { Citation, PlannerIntent, ShortlistOption } from "@/lib/types";
import type { PlannerSendResult } from "./use-planner-conversation";

// The store actions a finished planner turn writes to (a subset of PlannerActions).
export interface PlannerTurnSink {
  setIntent: (intent: Partial<PlannerIntent>) => void;
  setShortlist: (shortlist: ShortlistOption[], citations: Citation[]) => void;
  setNoOptionsMessage: (message: string | null) => void;
}

export const NO_OPTIONS_FALLBACK =
  "No options found matching your request in the current inventory.";

/**
 * The single assistant message a finished plan-mode turn adds to the
 * conversation (a clarify question appears exactly once, as this turn), or
 * null when the turn produced nothing to say.
 */
export function assistantTextForTurn(res: PlannerSendResult): string | null {
  if (res.error) return `Error: ${res.error}`;
  if (res.clarifyQuestion) return res.clarifyQuestion;
  if (res.shortlistReturned) {
    if (res.shortlist.length === 0) {
      return res.message || NO_OPTIONS_FALLBACK;
    }
    const n = res.shortlist.length;
    return (
      res.streamingText ||
      `Found ${n} option${n !== 1 ? "s" : ""} for your trip.`
    );
  }
  return res.streamingText || res.resultMessage || null;
}

/**
 * Apply a finished planner turn to the store:
 *  - the server's resolved intent fills the chips (it merged the prior turn,
 *    the chips and the message text, so it is authoritative);
 *  - a returned shortlist replaces the old one — including an EMPTY one, whose
 *    server message is surfaced in the recommendation panel.
 */
export function applyPlannerTurn(
  res: PlannerSendResult,
  sink: PlannerTurnSink,
): void {
  if (res.error || res.cancelled) return;
  if (res.intent) {
    sink.setIntent(res.intent as Partial<PlannerIntent>);
  }
  if (res.shortlistReturned) {
    sink.setShortlist(res.shortlist, res.citations);
    sink.setNoOptionsMessage(
      res.shortlist.length === 0 ? res.message || NO_OPTIONS_FALLBACK : null,
    );
  } else if (res.clarifyQuestion) {
    sink.setNoOptionsMessage(null);
  }
}
