import type {
  SSEFrame,
  ShortlistOption,
  Citation,
  TripChange,
} from "@/lib/types";
import { parseIntentSlots } from "./intent";

export function parseSSEFrame(event: string, data: string): SSEFrame | null {
  // Try to parse data as JSON; return null on malformed
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }

  if (!parsed || typeof parsed !== "object") return null;
  const d = parsed as Record<string, unknown>;

  switch (event) {
    case "status":
      return {
        event: "status",
        data: {
          task_type: String(d.task_type ?? ""),
          route: String(d.route ?? ""),
          confidence: Number(d.confidence ?? 0),
          status: String(d.status ?? ""),
        },
      };
    case "tool_activity":
      return { event: "tool_activity", data: { tool_calls: d.tool_calls } };
    case "result":
      return {
        event: "result",
        data: {
          output: (d.output as Record<string, unknown>) ?? {},
          citations: Array.isArray(d.citations)
            ? (d.citations as Citation[])
            : [],
        },
      };
    case "narrative":
      return { event: "narrative", data: { text: String(d.text ?? "") } };
    case "done":
      return {
        event: "done",
        data: {
          needs_review: Boolean(d.needs_review),
          message_id: d.message_id ? String(d.message_id) : null,
          sequence: Number(d.sequence ?? 0),
        },
      };
    default:
      return null;
  }
}

function nonEmptyString(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

function isCitation(v: unknown): v is Citation {
  return (
    !!v &&
    typeof v === "object" &&
    typeof (v as { ref?: unknown }).ref === "string"
  );
}

/**
 * Citations attached to the per-sentence breakdown in answer_structured,
 * de-duplicated by (kind, ref). Used when the frame's top-level citations are
 * missing so a needs_review draft still shows its sources.
 *
 * When `onlyIn` is given, only sentences whose text appears in it contribute:
 * answer_structured is the backend's UNFILTERED draft, and the provisional
 * `answer` keeps only the cited, entailment-passing sentences, so a dropped
 * sentence's citation must not be shown as a source of the surfaced text.
 */
export function citationsFromStructured(
  structured: unknown,
  onlyIn?: string,
): Citation[] {
  if (!structured || typeof structured !== "object") return [];
  const sentences = (structured as { sentences?: unknown }).sentences;
  if (!Array.isArray(sentences)) return [];
  const seen = new Set<string>();
  const out: Citation[] = [];
  for (const sentence of sentences) {
    if (onlyIn !== undefined) {
      const text =
        sentence && typeof sentence === "object"
          ? nonEmptyString((sentence as { text?: unknown }).text)
          : null;
      if (!text || !onlyIn.includes(text.trim())) continue;
    }
    const cites =
      sentence && typeof sentence === "object"
        ? (sentence as { citations?: unknown }).citations
        : null;
    if (!Array.isArray(cites)) continue;
    for (const c of cites) {
      if (!isCitation(c)) continue;
      const key = `${c.kind}|${c.ref}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(c);
    }
  }
  return out;
}

/**
 * The displayable text of a GROUNDED policy answer: `answer`, else
 * `answer_structured.full_text` (identical for a fully grounded answer).
 * Never used for needs_review — see discriminateOutput.
 */
export function policyAnswerText(
  output: Record<string, unknown>,
): string | null {
  const direct = nonEmptyString(output.answer);
  if (direct) return direct;
  const structured = output.answer_structured;
  if (structured && typeof structured === "object") {
    return nonEmptyString((structured as { full_text?: unknown }).full_text);
  }
  return null;
}

const TRIP_ACTIONS = new Set(["created", "updated", "unchanged"]);

function stringArray(v: unknown): string[] {
  return Array.isArray(v)
    ? v.filter((x): x is string => typeof x === "string")
    : [];
}

/**
 * UAT-2 Issue 4: what a selection did to the conversation's draft trip.
 * Defensive — anything malformed (or an unknown action) is null.
 */
export function parseTripChange(raw: unknown): TripChange | null {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.action !== "string" || !TRIP_ACTIONS.has(r.action)) return null;
  let previous: TripChange["previous_trip"] = null;
  if (r.previous_trip && typeof r.previous_trip === "object") {
    const p = r.previous_trip as Record<string, unknown>;
    if (typeof p.id === "string") {
      previous = {
        id: p.id,
        status: typeof p.status === "string" ? p.status : "",
        label: typeof p.label === "string" ? p.label : "",
        reason: typeof p.reason === "string" ? p.reason : "",
      };
    }
  }
  return {
    action: r.action as TripChange["action"],
    segment_types_added: stringArray(r.segment_types_added),
    segment_types_replaced: stringArray(r.segment_types_replaced),
    previous_trip: previous,
    estimated_cost:
      typeof r.estimated_cost === "string" ? r.estimated_cost : null,
    segment_count:
      typeof r.segment_count === "number" && Number.isFinite(r.segment_count)
        ? r.segment_count
        : 0,
    end_date_change: parseEndDateChange(r.end_date_change),
    stale_hotel: r.stale_hotel === true,
  };
}

function parseEndDateChange(raw: unknown): TripChange["end_date_change"] {
  if (!raw || typeof raw !== "object") return null;
  const r = raw as Record<string, unknown>;
  if (typeof r.from !== "string" || typeof r.to !== "string") return null;
  return { from: r.from, to: r.to };
}

// Discriminate the result output from a ResultFrame
export function discriminateOutput(
  output: Record<string, unknown>,
): import("@/lib/types").AssistantResultOutput | null {
  if (output.clarify === true && typeof output.question === "string") {
    return {
      clarify: true,
      question: output.question,
      intent: parseIntentSlots(output.intent),
    };
  }
  if (output.clarify === false && Array.isArray(output.shortlist)) {
    return {
      intent: parseIntentSlots(output.intent),
      clarify: false,
      shortlist: output.shortlist as ShortlistOption[],
      trip_id: output.trip_id ? String(output.trip_id) : null,
      reflection_outcome: output.reflection_outcome
        ? String(output.reflection_outcome)
        : null,
      degraded: Boolean(output.degraded),
      message: output.message ? String(output.message) : null,
      trip_change: parseTripChange(output.trip_change),
      trip_message: nonEmptyString(output.trip_message),
      trip_notice: nonEmptyString(output.trip_notice),
      trip_persist_error: nonEmptyString(output.trip_persist_error),
    };
  }
  if (output.answer_type === "grounded") {
    return {
      answer_type: "grounded",
      answer: policyAnswerText(output) ?? "",
      // The backend sends `asserted` as a BOOLEAN; only a pre-formatted string
      // is displayable (String(true) used to render a literal "true").
      asserted: typeof output.asserted === "string" ? output.asserted : "",
      critique: output.critique ? String(output.critique) : null,
    };
  }
  if (output.answer_type === "silent") {
    return {
      answer_type: "silent",
      reason: String(output.reason ?? ""),
      answer: null,
    };
  }
  // C7-07/08: a low-confidence policy answer. Per the shared contract the
  // backend puts the provisional text (critic-supported, cited, entailment-
  // passing sentences only) in `answer`, and sets it null when the critic
  // rejected the draft, the draft was uncited, or there was no draft. Show ONLY
  // `answer`: answer_structured.full_text is the unfiltered draft the backend
  // deliberately withheld, so it must never be surfaced (a null answer renders
  // the explicit "No confident answer" message, matching a reload).
  if (output.answer_type === "needs_review" || output.needs_review === true) {
    const answer = nonEmptyString(output.answer);
    return {
      answer_type: "needs_review",
      needs_review: true,
      answer,
      citations: answer
        ? citationsFromStructured(output.answer_structured, answer)
        : [],
      critique: output.critique ? String(output.critique) : null,
      message: nonEmptyString(output.message),
    };
  }
  return null;
}
