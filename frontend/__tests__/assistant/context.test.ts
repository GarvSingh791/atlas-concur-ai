import { describe, it, expect } from "vitest";
import { deriveContextFromPath } from "@/lib/assistant/context";

describe("deriveContextFromPath", () => {
  it("maps a trip detail route to travel_planning + ref id", () => {
    expect(deriveContextFromPath("/trips/abc-123")).toEqual({
      contextType: "travel_planning",
      contextRefId: "abc-123",
      contextLabel: "this trip",
    });
  });

  it("maps an expense detail route to expense + ref id", () => {
    expect(deriveContextFromPath("/expenses/rep-9")).toEqual({
      contextType: "expense",
      contextRefId: "rep-9",
      contextLabel: "this report",
    });
  });

  it("treats list / 'new' routes as global (general)", () => {
    expect(deriveContextFromPath("/trips").contextType).toBe("general");
    expect(deriveContextFromPath("/trips/new").contextType).toBe("general");
    expect(deriveContextFromPath("/expenses").contextType).toBe("general");
  });

  it("maps unrelated routes to general with no ref", () => {
    expect(deriveContextFromPath("/dashboard")).toEqual({
      contextType: "general",
      contextRefId: null,
      contextLabel: null,
    });
  });
});
