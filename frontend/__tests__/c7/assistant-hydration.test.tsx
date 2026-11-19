/**
 * C7-09 follow-up: router-persisted policy replies carry
 * citations = { citations, answer_type, needs_review, confidence,
 * confidence_source, answer_structured } and content = answer / abstention
 * text. A reloaded conversation must render the same trust surface: a
 * needs_review reply keeps its caution label; grounded / silent replies render
 * their text as before.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  renderHook,
  act,
} from "@testing-library/react";

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));

import { api } from "@/lib/api/client";
import { useAssistantConversation } from "@/lib/assistant/use-assistant-conversation";
import { useAssistantStore } from "@/stores/assistant-store";
import { AssistantTurn } from "@/components/atlas/assistant/assistant-turn";
import {
  NEEDS_REVIEW_LABEL,
  NEEDS_REVIEW_WITHHELD_LABEL,
  NO_CONFIDENT_ANSWER,
} from "@/components/planner/policy-answer";

const get = vi.mocked(api.get);

const PROVISIONAL = "Meals are reimbursed up to $75 per day.";
const GROUNDED = "Economy class is required for flights under 6 hours.";

function message(id: string, content: string, meta: Record<string, unknown>) {
  return {
    id,
    role: "assistant",
    content,
    tool_calls: null,
    citations: meta,
    sequence: 2,
    created_at: "2026-11-01T10:00:00Z",
  };
}

async function hydrate(messages: unknown[]) {
  get.mockResolvedValue({
    id: "conv-1",
    context_type: "policy_qa",
    context_ref_id: null,
    created_at: "",
    messages,
  } as never);
  const { result } = renderHook(() => useAssistantConversation());
  await act(async () => {
    await result.current.loadConversation("conv-1");
  });
  return useAssistantStore.getState().turns;
}

beforeEach(() => {
  useAssistantStore.getState().reset();
  sessionStorage.clear();
  get.mockReset();
});
afterEach(cleanup);

describe("assistant hydration of persisted policy replies", () => {
  it("a reloaded provisional needs_review reply shows the caution label and its text once", async () => {
    const turns = await hydrate([
      message("m-1", PROVISIONAL, {
        citations: [{ kind: "clause_ref", ref: "MEAL-02" }],
        answer_type: "needs_review",
        needs_review: true,
        confidence: 0.021,
        confidence_source: "calibrated",
        answer_structured: { full_text: PROVISIONAL },
      }),
    ]);
    expect(turns).toHaveLength(1);
    const t = turns[0];
    expect(t.answer?.answerType).toBe("needs_review");
    expect(t.answer?.answer).toBe(PROVISIONAL);
    expect(t.needsReview).toBe(true);
    expect(t.calibratedConfidence).toBe(0.021);
    expect(t.confidenceSource).toBe("calibrated");

    render(<AssistantTurn turn={t} onRetry={() => {}} />);
    expect(screen.getByText(NEEDS_REVIEW_LABEL)).toBeTruthy();
    expect(screen.getAllByText(PROVISIONAL)).toHaveLength(1);
    expect(screen.getByText(/MEAL-02/)).toBeTruthy();
  });

  it("a reloaded withheld needs_review reply shows the label and the no-answer message, no sources", async () => {
    const turns = await hydrate([
      message("m-2", NO_CONFIDENT_ANSWER, {
        citations: [],
        answer_type: "needs_review",
        needs_review: true,
        confidence: 0.01,
        confidence_source: "calibrated",
      }),
    ]);
    const t = turns[0];
    expect(t.answer).toEqual({ answerType: "needs_review", answer: null });
    render(<AssistantTurn turn={t} onRetry={() => {}} />);
    // C7R: no sources are shown, so the label must not say "verify against the
    // cited policy clause".
    expect(screen.getByText(NEEDS_REVIEW_WITHHELD_LABEL)).toBeTruthy();
    expect(screen.queryByText(NEEDS_REVIEW_LABEL)).toBeNull();
    expect(screen.getAllByText(NO_CONFIDENT_ANSWER)).toHaveLength(1);
  });

  it("a reloaded grounded reply renders its text as before, without a caution label", async () => {
    const turns = await hydrate([
      message("m-3", GROUNDED, {
        citations: [{ kind: "clause_ref", ref: "TRV-01" }],
        answer_type: "grounded",
        needs_review: false,
        confidence: 0.2,
        confidence_source: "calibrated",
      }),
    ]);
    const t = turns[0];
    expect(t.answer).toBeUndefined();
    expect(t.content).toBe(GROUNDED);
    expect(t.citations).toEqual([{ kind: "clause_ref", ref: "TRV-01" }]);
    render(<AssistantTurn turn={t} onRetry={() => {}} />);
    expect(screen.getByText(GROUNDED)).toBeTruthy();
    expect(screen.queryByText(NEEDS_REVIEW_LABEL)).toBeNull();
  });

  it("a reloaded silent reply and a legacy { citations } message render their content", async () => {
    const turns = await hydrate([
      message("m-4", "The policy does not cover pet travel.", {
        citations: [],
        answer_type: "silent",
        needs_review: false,
        confidence: null,
        confidence_source: null,
      }),
      message("m-5", "Trip created.", { citations: [] }),
    ]);
    expect(turns.map((t) => t.content)).toEqual([
      "The policy does not cover pet travel.",
      "Trip created.",
    ]);
    expect(turns.every((t) => t.answer === undefined)).toBe(true);
    expect(turns.every((t) => !t.needsReview)).toBe(true);
  });
});
