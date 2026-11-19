import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { AssistantTurn } from "@/components/atlas/assistant/assistant-turn";
import type { AssistantTurn as Turn } from "@/stores/assistant-store";

afterEach(cleanup);

const base = {
  id: "t1",
  timestamp: "2026-01-01T00:00:00Z",
};

function renderTurn(turn: Turn, onRetry = vi.fn()) {
  render(<AssistantTurn turn={turn} onRetry={onRetry} />);
  return onRetry;
}

describe("AssistantTurn", () => {
  it("renders a user message", () => {
    renderTurn({
      ...base,
      role: "user",
      content: "Is business class allowed?",
    });
    expect(screen.getByText(/Is business class allowed/i)).toBeTruthy();
  });

  it("renders a grounded answer with citations", () => {
    renderTurn({
      ...base,
      role: "assistant",
      content: "",
      answer: {
        answerType: "grounded",
        answer: "Yes, for flights over 6 hours.",
        asserted: "Travel Policy §4.1",
        critique: null,
      },
      citations: [{ kind: "clause_ref", ref: "Travel Policy §4.1" }],
      route: "llm",
      confidence: 88,
    });
    expect(screen.getByText(/flights over 6 hours/i)).toBeTruthy();
    // asserted <p> still renders the pre-formatted ref; the chip now renders a
    // document-qualified label built from documentType + bare ref.
    expect(screen.getByText("Travel Policy §4.1")).toBeTruthy();
    // grounding confidence cue (llm route) — relabelled "Grounding confidence"
    expect(screen.getByText(/Grounding confidence/i)).toBeTruthy();
    expect(screen.getByText(/88%/)).toBeTruthy();
  });

  it("renders 'policy is silent' faithfully", () => {
    renderTurn({
      ...base,
      role: "assistant",
      content: "",
      answer: {
        answerType: "silent",
        answer: null,
        reason: "No matching clause",
      },
      citations: [],
    });
    expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy();
  });

  it("renders a clear role-bounded refusal (NFR-SEC6)", () => {
    renderTurn({
      ...base,
      role: "assistant",
      content: "",
      // C7-08: refusals now use the dedicated "denied" answer type;
      // "needs_review" is the low-confidence policy-answer state.
      answer: {
        answerType: "denied",
        answer: null,
        reason: "Action 'trip_create' requires permission 'trip.create'.",
      },
      needsReview: true,
    });
    expect(screen.getByText(/can't complete that for your role/i)).toBeTruthy();
    expect(screen.getByText(/requires permission/i)).toBeTruthy();
  });

  it("shows a 'Deterministic' badge instead of a % for the deterministic route", () => {
    renderTurn({
      ...base,
      role: "assistant",
      content: "",
      answer: { answerType: "silent", answer: null, reason: "" },
      route: "deterministic",
      confidence: 99,
    });
    expect(screen.getByText("Deterministic")).toBeTruthy();
    expect(screen.queryByText(/Confidence:/i)).toBeNull();
  });

  it("renders an interrupted turn with a working Retry button", () => {
    const onRetry = renderTurn({
      ...base,
      role: "assistant",
      content: "",
      error: "Response interrupted.",
    });
    expect(screen.getByText(/Response interrupted/i)).toBeTruthy();
    fireEvent.click(screen.getByText(/Retry/i));
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("renders the tool-activity summary, expandable to real tool steps", () => {
    renderTurn({
      ...base,
      role: "assistant",
      content: "",
      answer: { answerType: "silent", answer: null, reason: "" },
      toolActivity: [
        {
          id: "InventoryTool.flights",
          name: "Inventory",
          detail: "flights",
          status: "complete",
        },
        { id: "PolicyEvalTool", name: "Policy Evaluation", status: "complete" },
      ],
    });
    expect(screen.getByText(/2 tools used/i)).toBeTruthy();
    fireEvent.click(screen.getByText(/2 tools used/i));
    expect(screen.getByText("Policy Evaluation")).toBeTruthy();
  });
});
