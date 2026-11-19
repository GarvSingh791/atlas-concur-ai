import { describe, it, expect, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

afterEach(cleanup);

import {
  NEEDS_REVIEW_LABEL,
  NEEDS_REVIEW_WITHHELD_LABEL,
  PolicyAnswer,
  silentReasonText,
} from "@/components/planner/policy-answer";

describe("PolicyAnswer", () => {
  it("renders grounded answer with text and citations", () => {
    render(
      <PolicyAnswer
        answerType="grounded"
        answer="Business class is allowed on flights over 6 hours."
        asserted="Travel Policy §4.1"
        critique={null}
        citations={[{ kind: "policy", ref: "Travel Policy §4.1" }]}
      />,
    );
    expect(screen.getByText(/Business class is allowed/i)).toBeTruthy();
    // "Travel Policy §4.1" appears in both the asserted <p> and the citation chip button
    expect(
      screen.getAllByText("Travel Policy §4.1").length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("renders silent answer with policy is silent message", () => {
    render(
      <PolicyAnswer
        answerType="silent"
        answer={null}
        reason="No matching clause"
        citations={[]}
      />,
    );
    expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy();
  });

  it("renders needs_review state", () => {
    render(
      <PolicyAnswer answerType="needs_review" answer={null} citations={[]} />,
    );
    // C7-07/08: a needs_review answer with no draft shows an explicit
    // no-confident-answer message + caution label (was a generic placeholder).
    expect(screen.getByText(/No confident answer/i)).toBeTruthy();
    expect(screen.getByText(/Needs review — low confidence/i)).toBeTruthy();
    // C7R: a withheld answer shows no sources, so the label must not send the
    // user to "the cited policy clause".
    expect(screen.getByText(NEEDS_REVIEW_WITHHELD_LABEL)).toBeTruthy();
    expect(screen.queryByText(NEEDS_REVIEW_LABEL)).toBeNull();
  });

  it("a provisional needs_review answer keeps the cited-clause label", () => {
    render(
      <PolicyAnswer
        answerType="needs_review"
        answer="Meals are reimbursed up to $75 per day."
        citations={[{ kind: "clause_ref", ref: "MEAL-02" }]}
      />,
    );
    expect(screen.getByText(NEEDS_REVIEW_LABEL)).toBeTruthy();
    expect(screen.queryByText(NEEDS_REVIEW_WITHHELD_LABEL)).toBeNull();
  });

  it("never shows an internal reason code (C7R)", () => {
    const { rerender } = render(
      <PolicyAnswer
        answerType="silent"
        answer={null}
        reason="model_declined"
        citations={[]}
      />,
    );
    expect(screen.queryByText(/model_declined/)).toBeNull();
    expect(
      screen.getByText(/The policy documents do not address this question/),
    ).toBeTruthy();

    rerender(
      <PolicyAnswer
        answerType="silent"
        answer={null}
        reason="below_threshold: max_sim=0.12 < 0.20"
        citations={[]}
      />,
    );
    expect(screen.queryByText(/below_threshold/)).toBeNull();
    expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy();
  });

  it("silentReasonText maps codes, hides unknown codes and keeps prose", () => {
    expect(silentReasonText("model_declined")).toMatch(/do not address/);
    expect(silentReasonText("no_verified_citation")).toMatch(
      /No policy clause/,
    );
    expect(silentReasonText("below_threshold: max_sim=0.1")).toBe("");
    expect(silentReasonText("some_new_code")).toBe("");
    expect(silentReasonText("No matching clause")).toBe("No matching clause");
    expect(silentReasonText(undefined)).toBe("");
  });
});
