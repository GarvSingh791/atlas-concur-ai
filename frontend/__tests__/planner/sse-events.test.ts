import { describe, it, expect } from "vitest";
import { parseSSEFrame, discriminateOutput } from "@/lib/planner/sse-events";

describe("parseSSEFrame", () => {
  it("parses status frame", () => {
    const frame = parseSSEFrame(
      "status",
      JSON.stringify({
        task_type: "travel",
        route: "deterministic",
        confidence: 0.95,
        status: "success",
      }),
    );
    expect(frame).not.toBeNull();
    expect(frame?.event).toBe("status");
    if (frame?.event === "status") {
      expect(frame.data.confidence).toBe(0.95);
      expect(frame.data.route).toBe("deterministic");
    }
  });

  it("parses result frame", () => {
    const frame = parseSSEFrame(
      "result",
      JSON.stringify({
        output: {
          clarify: false,
          shortlist: [],
          trip_id: null,
          clarify_false: false,
          degraded: false,
          message: null,
        },
        citations: [],
      }),
    );
    expect(frame?.event).toBe("result");
  });

  it("parses narrative frame", () => {
    const frame = parseSSEFrame(
      "narrative",
      JSON.stringify({ text: "Hello world" }),
    );
    expect(frame?.event).toBe("narrative");
    if (frame?.event === "narrative") {
      expect(frame.data.text).toBe("Hello world");
    }
  });

  it("parses done frame", () => {
    const frame = parseSSEFrame(
      "done",
      JSON.stringify({ needs_review: false, message_id: "abc", sequence: 1 }),
    );
    expect(frame?.event).toBe("done");
    if (frame?.event === "done") {
      expect(frame.data.sequence).toBe(1);
    }
  });

  it("parses tool_activity frame", () => {
    const frame = parseSSEFrame(
      "tool_activity",
      JSON.stringify({ tool_calls: [{ name: "InventoryTool" }] }),
    );
    expect(frame?.event).toBe("tool_activity");
  });

  it("returns null for malformed data", () => {
    const frame = parseSSEFrame("status", "not-valid-json{{");
    expect(frame).toBeNull();
  });

  it("returns null for unknown event type", () => {
    const frame = parseSSEFrame(
      "unknown_event",
      JSON.stringify({ foo: "bar" }),
    );
    expect(frame).toBeNull();
  });
});

describe("discriminateOutput", () => {
  it("discriminates clarify output", () => {
    const output = { clarify: true, question: "Where are you flying from?" };
    const result = discriminateOutput(output);
    expect(result).not.toBeNull();
    if (result && "clarify" in result && result.clarify) {
      expect(result.question).toBe("Where are you flying from?");
    }
  });

  it("discriminates shortlist output", () => {
    const output = {
      clarify: false,
      shortlist: [],
      trip_id: null,
      reflection_outcome: null,
      degraded: false,
      message: null,
    };
    const result = discriminateOutput(output);
    expect(result).not.toBeNull();
    if (result && "clarify" in result && !result.clarify) {
      expect(result.shortlist).toEqual([]);
    }
  });

  it("discriminates grounded policy output", () => {
    const output = {
      answer_type: "grounded",
      answer: "Yes",
      asserted: "Policy §4.1",
      critique: null,
    };
    const result = discriminateOutput(output);
    expect(result).not.toBeNull();
    if (
      result &&
      "answer_type" in result &&
      result.answer_type === "grounded"
    ) {
      expect(result.answer).toBe("Yes");
    }
  });

  it("discriminates silent policy output", () => {
    const output = {
      answer_type: "silent",
      reason: "No matching clause",
      answer: null,
    };
    const result = discriminateOutput(output);
    expect(result).not.toBeNull();
    if (result && "answer_type" in result && result.answer_type === "silent") {
      expect(result.answer).toBeNull();
    }
  });
});
