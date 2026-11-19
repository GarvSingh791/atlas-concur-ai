import { describe, it, expect, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { AssistantTurn } from "@/components/atlas/assistant/assistant-turn";
import { PolicyAnswer } from "@/components/planner/policy-answer";
import type { AssistantTurn as Turn } from "@/stores/assistant-store";

afterEach(cleanup);

const base = { id: "t1", timestamp: "2026-01-01T00:00:00Z" };

describe("Grounding confidence (calibrated, tau*-normalized)", () => {
  it("relabels the policy confidence 'Grounding confidence' and renders non-red for a grounded (above-threshold) answer", () => {
    // Calibrated 0.06 → display 72.5% (amber). The hook stores the transformed
    // value on turn.confidence; here we simulate that stored value (72/73).
    render(
      <AssistantTurn
        turn={
          {
            ...base,
            role: "assistant",
            content: "",
            answer: {
              answerType: "grounded",
              answer: "Economy is the default cabin.",
              asserted: "T&E Policy §AIR-01",
              critique: null,
            },
            citations: [
              { kind: "clause_ref", ref: "AIR-01", document_type: "policy" },
            ],
            route: "llm",
            confidence: 73, // display value (amber, NOT red)
            calibratedConfidence: 0.06,
            confidenceSource: "calibrated",
            threshold: 0.039299,
          } as Turn
        }
        onRetry={() => {}}
      />,
    );
    expect(screen.getByText(/Grounding confidence/i)).toBeTruthy();
    // The meter reflects a non-red banding (>= 70 amber). role="meter" from
    // the unchanged ConfidenceIndicator.
    const meter = screen.getByRole("meter");
    expect(Number(meter.getAttribute("aria-valuenow"))).toBeGreaterThanOrEqual(
      70,
    );
  });

  it("exposes the raw calibrated score + tau* in a focusable tooltip", async () => {
    render(
      <AssistantTurn
        turn={
          {
            ...base,
            role: "assistant",
            content: "",
            answer: {
              answerType: "grounded",
              answer: "Economy is the default cabin.",
              asserted: "T&E Policy §AIR-01",
              critique: null,
            },
            citations: [],
            route: "llm",
            confidence: 73,
            calibratedConfidence: 0.06,
            confidenceSource: "calibrated",
            threshold: 0.039299,
          } as Turn
        }
        onRetry={() => {}}
      />,
    );
    const trigger = screen.getByRole("button", {
      name: /about grounding confidence/i,
    });
    fireEvent.focus(trigger);
    expect(
      (await screen.findAllByText(/Calibrated relevance: 0\.0600/i)).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/0\.039299/).length).toBeGreaterThanOrEqual(1);
  });
});

describe("Silent-state escalation CTA (non-alarming)", () => {
  it("renders the escalation message as a semantic, info-styled CTA plus near-topic", () => {
    render(
      <PolicyAnswer
        answerType="silent"
        answer={null}
        reason=""
        citations={[]}
        escalation={{
          message: "Ask your finance team",
          secondary: "finance@atlas.internal",
        }}
        nearTopic="international per-diem rates"
      />,
    );
    expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy();
    // CTA is a button (semantic, focusable) with trust vocab — NOT an error.
    expect(
      screen.getByRole("button", { name: /Ask your finance team/i }),
    ).toBeTruthy();
    expect(screen.getByText(/finance@atlas.internal/i)).toBeTruthy();
    expect(screen.getByText(/international per-diem rates/i)).toBeTruthy();
  });

  it("stays calm with no escalation payload", () => {
    render(
      <PolicyAnswer
        answerType="silent"
        answer={null}
        reason=""
        citations={[]}
      />,
    );
    expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
