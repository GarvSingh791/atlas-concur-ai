import { describe, it, expect } from "vitest";
import {
  discriminateOutput,
  citationsFromStructured,
  policyAnswerText,
} from "@/lib/planner/sse-events";
import {
  STAGING_NEEDS_REVIEW_OUTPUT,
  CONTRACT_NEEDS_REVIEW_OUTPUT,
  EMPTY_NEEDS_REVIEW_OUTPUT,
  REJECTED_NEEDS_REVIEW_OUTPUT,
  PARTIAL_PROVISIONAL_OUTPUT,
  PROVISIONAL_KEPT,
  MEALS_TEXT,
  MEALS_CITE,
} from "./needs-review-fixtures";

describe("C7-07/08 discriminateOutput — needs_review", () => {
  it("staging shape (answer null): never surfaces answer_structured.full_text", () => {
    const d = discriminateOutput(STAGING_NEEDS_REVIEW_OUTPUT);
    // Old code returned null here (no needs_review branch) → blank UI.
    expect(d).not.toBeNull();
    expect(d && "answer_type" in d && d.answer_type).toBe("needs_review");
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBeNull();
      expect(d.needs_review).toBe(true);
      expect(d.citations).toEqual([]);
    }
  });

  it("critic-rejected draft (answer null, full_text present) is NOT shown", () => {
    const d = discriminateOutput(REJECTED_NEEDS_REVIEW_OUTPUT);
    expect(d && "answer_type" in d && d.answer_type).toBe("needs_review");
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBeNull();
      expect(d.citations).toEqual([]);
      expect(JSON.stringify(d)).not.toContain("Alcohol");
    }
  });

  it("provisional answer: structured citations only from sentences kept in `answer`", () => {
    const d = discriminateOutput(PARTIAL_PROVISIONAL_OUTPUT);
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBe(PROVISIONAL_KEPT);
      expect(d.citations).toEqual([MEALS_CITE]);
    } else {
      throw new Error("expected needs_review");
    }
  });

  it("contract shape (answer populated + needs_review true) uses `answer`", () => {
    const d = discriminateOutput(CONTRACT_NEEDS_REVIEW_OUTPUT);
    expect(d && "answer_type" in d && d.answer_type).toBe("needs_review");
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBe(MEALS_TEXT);
      expect(d.citations).toEqual([MEALS_CITE]);
    }
  });

  it("needs_review: true without answer_type is still recognised", () => {
    const d = discriminateOutput({ needs_review: true, answer: "Draft." });
    expect(d && "answer_type" in d && d.answer_type).toBe("needs_review");
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBe("Draft.");
    }
  });

  it("no draft → answer null (UI shows the explicit no-answer message)", () => {
    const d = discriminateOutput(EMPTY_NEEDS_REVIEW_OUTPUT);
    expect(d && "answer_type" in d && d.answer_type).toBe("needs_review");
    if (d && "answer_type" in d && d.answer_type === "needs_review") {
      expect(d.answer).toBeNull();
      expect(d.citations).toEqual([]);
    }
  });

  it("grounded: boolean `asserted` from the backend never renders as 'true'", () => {
    const d = discriminateOutput({
      answer_type: "grounded",
      answer: "USD 250 per night [Policy §HOTEL-CAP].",
      asserted: true,
      critique: null,
    });
    expect(d && "answer_type" in d && d.answer_type).toBe("grounded");
    if (d && "answer_type" in d && d.answer_type === "grounded") {
      expect(d.answer).toBe("USD 250 per night [Policy §HOTEL-CAP].");
      expect(d.asserted).toBe("");
    }
  });

  it("silent/abstain and denied shapes are unchanged", () => {
    const s = discriminateOutput({
      answer_type: "silent",
      answer: null,
      reason: "below_threshold",
    });
    expect(s).toEqual({
      answer_type: "silent",
      reason: "below_threshold",
      answer: null,
    });
    expect(discriminateOutput({ denied: true, reason: "nope" })).toBeNull();
  });

  it("policyAnswerText / citationsFromStructured tolerate junk", () => {
    expect(policyAnswerText({ answer: "  ", answer_structured: 5 })).toBeNull();
    expect(citationsFromStructured(null)).toEqual([]);
    expect(
      citationsFromStructured({ sentences: [{ citations: [{}] }] }),
    ).toEqual([]);
  });
});
