/**
 * Route → assistant-context mapping (P5-T10).
 *
 * Backend context_type has no 'trip'/'report' value — it is
 * travel_planning / expense / policy_qa / general. We map:
 *   /trips/{id}    → travel_planning + context_ref_id
 *   /expenses/{id} → expense + context_ref_id
 *   anything else  → general (policy Q&A still routes correctly server-side)
 *
 * Detail routes (/trips/[id], /expenses/[id]) are stubbed today, so global
 * routes resolve to `general`; scoped sessions are also reachable via the
 * imperative store API (openScoped) from any screen.
 */

import type { AssistantContext } from "@/stores/assistant-store";

export function deriveContextFromPath(pathname: string): AssistantContext {
  const trip = pathname.match(/^\/trips\/([^/]+)/);
  if (trip && trip[1] && trip[1] !== "new") {
    return {
      contextType: "travel_planning",
      contextRefId: trip[1],
      contextLabel: "this trip",
    };
  }

  const report = pathname.match(/^\/expenses\/([^/]+)/);
  if (report && report[1] && report[1] !== "new") {
    return {
      contextType: "expense",
      contextRefId: report[1],
      contextLabel: "this report",
    };
  }

  return { contextType: "general", contextRefId: null, contextLabel: null };
}
