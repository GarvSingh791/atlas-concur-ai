import type { PlannerIntent, PlannerIntentSlots } from "@/lib/types";

// Default empty intent
export const EMPTY_INTENT: PlannerIntent = {
  origin: null,
  destination: null,
  departure_date: null,
  return_date: null,
  travelers: null,
  currency: "USD",
  purpose: null,
  include_flights: true,
  include_hotels: true,
  constraints: {},
};

const SLOT_KEYS = [
  "origin",
  "destination",
  "departure_date",
  "return_date",
  "travelers",
  "currency",
  "purpose",
] as const;

// Validate the server's output.intent into chip slots. Unknown keys and
// wrongly-typed values are dropped; a missing key leaves the chip untouched.
export function parseIntentSlots(raw: unknown): PlannerIntentSlots | null {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const src = raw as Record<string, unknown>;
  const out: Record<string, string | number | null> = {};
  for (const key of SLOT_KEYS) {
    if (!(key in src)) continue;
    const val = src[key];
    if (val === null) {
      out[key] = null;
    } else if (key === "travelers") {
      const n = Number(val);
      if (Number.isInteger(n) && n >= 1) out[key] = n;
    } else if (typeof val === "string" && val.trim() !== "") {
      out[key] = val.trim();
    }
  }
  return out as PlannerIntentSlots;
}

// True when every user-facing slot is empty (e.g. a fresh tab after reload).
export function isIntentEmpty(intent: PlannerIntent): boolean {
  return SLOT_KEYS.every((key) => {
    if (key === "currency") return true; // defaulted client-side
    const val = intent[key];
    return val === null || val === undefined || val === "";
  });
}

// Canonical required-slot order (mirrors backend _REQUIRED_SLOTS)
// Used for chip highlighting only — server generates the clarify question
const REQUIRED_SLOTS = [
  "origin",
  "destination",
  "departure_date",
  "travelers",
  "currency",
] as const;
type RequiredSlot = (typeof REQUIRED_SLOTS)[number];

// Returns the first missing required slot, or null if all are filled
export function missingRequiredSlot(
  intent: PlannerIntent,
): RequiredSlot | null {
  for (const slot of REQUIRED_SLOTS) {
    const val = intent[slot];
    if (
      val === null ||
      val === undefined ||
      (typeof val === "string" && val.trim() === "")
    ) {
      return slot;
    }
  }
  return null;
}

// Build the message payload to send to the backend
export function toMessagePayload(
  intent: PlannerIntent,
  opts: {
    selectedOptionIds?: string[];
    explainWithLlm?: boolean;
    content?: string;
    conversationId?: string;
    // "chips": the turn is a structured chip edit — the server skips text extraction
    source?: "chips";
  } = {},
): {
  content: string;
  payload: Record<string, unknown>;
  explain_with_llm: boolean;
} {
  return {
    content: opts.content ?? "Plan my trip",
    payload: {
      origin: intent.origin ?? undefined,
      destination: intent.destination ?? undefined,
      departure_date: intent.departure_date ?? undefined,
      // Optional slots are sent as an explicit null when empty: the server
      // merges onto the conversation's previous intent, and null is how a
      // removed Return/Purpose chip clears it there (omitted = "keep").
      return_date: intent.return_date ?? null,
      travelers: intent.travelers ?? undefined,
      currency: intent.currency ?? undefined,
      purpose: intent.purpose ?? null,
      include_flights: intent.include_flights,
      include_hotels: intent.include_hotels,
      constraints:
        Object.keys(intent.constraints).length > 0
          ? intent.constraints
          : undefined,
      ...(opts.selectedOptionIds?.length
        ? { selected_option_ids: opts.selectedOptionIds }
        : {}),
      ...(opts.conversationId ? { conversation_id: opts.conversationId } : {}),
      ...(opts.source ? { source: opts.source } : {}),
    },
    explain_with_llm: opts.explainWithLlm ?? false,
  };
}

// Chip metadata for rendering intent chips in the UI
export interface ChipMeta {
  key: keyof PlannerIntent;
  label: string;
  color: "blue" | "green" | "amber" | "gray";
  required: boolean;
  format: (val: PlannerIntent[keyof PlannerIntent]) => string;
}

export const CHIP_METADATA: ChipMeta[] = [
  {
    key: "origin",
    label: "From",
    color: "blue",
    required: true,
    format: (v) => String(v ?? ""),
  },
  {
    key: "destination",
    label: "To",
    color: "blue",
    required: true,
    format: (v) => String(v ?? ""),
  },
  {
    key: "departure_date",
    label: "Depart",
    color: "green",
    required: true,
    format: (v) => String(v ?? ""),
  },
  {
    key: "return_date",
    label: "Return",
    color: "green",
    required: false,
    format: (v) => String(v ?? ""),
  },
  {
    key: "travelers",
    label: "Travelers",
    color: "gray",
    required: true,
    format: (v) =>
      v !== null && v !== undefined
        ? `${v} traveler${Number(v) !== 1 ? "s" : ""}`
        : "",
  },
  {
    key: "currency",
    label: "Currency",
    color: "gray",
    required: true,
    format: (v) => String(v ?? ""),
  },
  {
    key: "purpose",
    label: "Purpose",
    color: "amber",
    required: false,
    format: (v) => String(v ?? ""),
  },
];
