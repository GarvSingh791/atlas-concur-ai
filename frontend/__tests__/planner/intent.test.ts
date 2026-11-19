import { describe, it, expect } from "vitest";
import {
  toMessagePayload,
  missingRequiredSlot,
  parseIntentSlots,
  isIntentEmpty,
  CHIP_METADATA,
  EMPTY_INTENT,
} from "@/lib/planner/intent";
import type { PlannerIntent } from "@/lib/types";

const FULL_INTENT: PlannerIntent = {
  origin: "SFO",
  destination: "JFK",
  departure_date: "2026-07-01",
  return_date: "2026-07-05",
  travelers: 1,
  currency: "USD",
  include_flights: true,
  include_hotels: true,
  constraints: {},
};

describe("missingRequiredSlot", () => {
  it("returns null when all required slots filled", () => {
    expect(missingRequiredSlot(FULL_INTENT)).toBeNull();
  });

  it("returns the first missing slot", () => {
    const partial = { ...FULL_INTENT, origin: null };
    expect(missingRequiredSlot(partial)).toBe("origin");
  });

  it("returns destination when origin is set but destination is null", () => {
    const partial = { ...FULL_INTENT, destination: null };
    expect(missingRequiredSlot(partial)).toBe("destination");
  });

  it("EMPTY_INTENT returns origin as first missing", () => {
    expect(missingRequiredSlot(EMPTY_INTENT)).toBe("origin");
  });
});

describe("toMessagePayload", () => {
  it("includes all filled intent slots in payload", () => {
    const result = toMessagePayload(FULL_INTENT);
    expect(result.payload.origin).toBe("SFO");
    expect(result.payload.destination).toBe("JFK");
    expect(result.payload.departure_date).toBe("2026-07-01");
    expect(result.payload.travelers).toBe(1);
    expect(result.payload.currency).toBe("USD");
  });

  it("omits null required slots from payload", () => {
    const intent = { ...FULL_INTENT, origin: null, travelers: null };
    const result = toMessagePayload(intent);
    expect(result.payload.origin).toBeUndefined();
    expect(result.payload.travelers).toBeUndefined();
  });

  // The server merges each turn onto the conversation's previous intent, where
  // an omitted key means "keep". An explicit null is how a removed Return /
  // Purpose chip clears the server-side value (it used to be omitted).
  it("sends empty optional slots as explicit null so they clear server-side", () => {
    const intent = { ...FULL_INTENT, return_date: null, purpose: null };
    const result = toMessagePayload(intent);
    expect(result.payload.return_date).toBeNull();
    expect(result.payload.purpose).toBeNull();
    expect("return_date" in result.payload).toBe(true);
  });

  it("marks chip edits with source=chips (server skips text extraction)", () => {
    expect(
      toMessagePayload(FULL_INTENT, { source: "chips" }).payload.source,
    ).toBe("chips");
    expect(toMessagePayload(FULL_INTENT).payload.source).toBeUndefined();
  });

  it("includes selected_option_ids when provided", () => {
    const result = toMessagePayload(FULL_INTENT, {
      selectedOptionIds: ["opt-123"],
    });
    expect(result.payload.selected_option_ids).toEqual(["opt-123"]);
  });

  it("does not include selected_option_ids when not provided", () => {
    const result = toMessagePayload(FULL_INTENT);
    expect(result.payload.selected_option_ids).toBeUndefined();
  });

  it("sets explain_with_llm from opts", () => {
    const result = toMessagePayload(FULL_INTENT, { explainWithLlm: true });
    expect(result.explain_with_llm).toBe(true);
  });

  it("defaults explain_with_llm to false", () => {
    const result = toMessagePayload(FULL_INTENT);
    expect(result.explain_with_llm).toBe(false);
  });
});

describe("parseIntentSlots", () => {
  it("keeps known slots (including explicit nulls) and drops the rest", () => {
    expect(
      parseIntentSlots({
        origin: "ORD",
        destination: "JFK",
        departure_date: "2026-11-17",
        return_date: "2026-11-19",
        travelers: 1,
        currency: "USD",
        purpose: "Client meeting",
        unknown: "x",
      }),
    ).toEqual({
      origin: "ORD",
      destination: "JFK",
      departure_date: "2026-11-17",
      return_date: "2026-11-19",
      travelers: 1,
      currency: "USD",
      purpose: "Client meeting",
    });
    expect(parseIntentSlots({ origin: null })).toEqual({ origin: null });
  });

  it("drops wrongly-typed values and rejects non-objects", () => {
    expect(parseIntentSlots({ travelers: 0, origin: 42 })).toEqual({});
    expect(parseIntentSlots(null)).toBeNull();
    expect(parseIntentSlots("ORD")).toBeNull();
    expect(parseIntentSlots(["ORD"])).toBeNull();
  });
});

describe("isIntentEmpty", () => {
  it("is true for the default intent (currency is a client default)", () => {
    expect(isIntentEmpty(EMPTY_INTENT)).toBe(true);
  });
  it("is false once any trip slot is set", () => {
    expect(isIntentEmpty({ ...EMPTY_INTENT, origin: "ORD" })).toBe(false);
  });
});

describe("CHIP_METADATA", () => {
  it("exposes optional Return and Purpose chips", () => {
    const optional = CHIP_METADATA.filter((m) => !m.required).map((m) => m.key);
    expect(optional).toEqual(["return_date", "purpose"]);
  });
});
