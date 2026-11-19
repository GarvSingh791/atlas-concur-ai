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

import { streamMessages } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import {
  buildResultPatch,
  useAssistantConversation,
} from "@/lib/assistant/use-assistant-conversation";
import { useAssistantStore } from "@/stores/assistant-store";
import { AssistantTurn } from "@/components/atlas/assistant/assistant-turn";
import {
  STAGING_NEEDS_REVIEW_OUTPUT,
  CONTRACT_NEEDS_REVIEW_OUTPUT,
  EMPTY_NEEDS_REVIEW_OUTPUT,
  REJECTED_NEEDS_REVIEW_OUTPUT,
  REJECTED_TEXT,
  MEALS_TEXT,
  SSE_DONE,
  sseResult,
} from "./needs-review-fixtures";

const stream = vi.mocked(streamMessages);
const post = vi.mocked(api.post);
const get = vi.mocked(api.get);

const GENERAL_STATUS = JSON.stringify({
  task_type: "general_query",
  route: "llm",
  confidence: 0.4,
  status: "needs_review",
});

beforeEach(() => {
  useAssistantStore.getState().reset();
  sessionStorage.clear();
  stream.mockReset();
  post.mockReset();
  get.mockReset();
  post.mockResolvedValue({ id: "conv-1" } as never);
  get.mockResolvedValue({ id: "conv-1", messages: [] } as never);
});
afterEach(cleanup);

async function runTurn(output: Record<string, unknown>, withCitations = true) {
  stream.mockImplementation((_id, _body, cbs) => {
    cbs.onEvent?.("status", GENERAL_STATUS);
    cbs.onEvent?.(
      "result",
      withCitations ? sseResult(output) : sseResult(output, []),
    );
    cbs.onEvent?.("done", SSE_DONE);
    cbs.onComplete();
    return () => {};
  });
  const { result } = renderHook(() => useAssistantConversation());
  await act(async () => {
    await result.current.sendMessage("What is the daily meal per diem?");
  });
  const turn = useAssistantStore.getState().turns[1];
  render(<AssistantTurn turn={turn} onRetry={vi.fn()} />);
  return turn;
}

describe("C7-08 buildResultPatch", () => {
  it("returns text + needsReview for the contract needs_review shape (was {})", () => {
    const patch = buildResultPatch(CONTRACT_NEEDS_REVIEW_OUTPUT);
    expect(patch.answer?.answerType).toBe("needs_review");
    expect(patch.answer?.answer).toBe(MEALS_TEXT);
    expect(patch.needsReview).toBe(true);
    expect(patch.structuredCitations?.[0]?.ref).toBe("MEALS-PD");
  });

  it("staging shape (answer null): needs_review with NO text (full_text withheld)", () => {
    const patch = buildResultPatch(STAGING_NEEDS_REVIEW_OUTPUT);
    expect(patch.answer?.answerType).toBe("needs_review");
    expect(patch.answer?.answer ?? null).toBeNull();
    expect(patch.needsReview).toBe(true);
    expect(patch.structuredCitations ?? []).toEqual([]);
  });

  it("role refusals map to the dedicated 'denied' type", () => {
    const patch = buildResultPatch({ denied: true, reason: "no perm" });
    expect(patch.answer?.answerType).toBe("denied");
    expect(patch.answer?.reason).toBe("no perm");
  });
});

describe("C7-08 Atlas Assistant renders needs_review answers", () => {
  it("contract shape: shows the provisional cited text + caution label + citation", async () => {
    const turn = await runTurn(CONTRACT_NEEDS_REVIEW_OUTPUT);
    expect(turn.needsReview).toBe(true);
    expect(screen.getByText(MEALS_TEXT)).toBeTruthy();
    expect(screen.getByText(/Needs review — low confidence/i)).toBeTruthy();
    expect(screen.getAllByText(/MEALS-PD/).length).toBeGreaterThan(0);
    // Not mistaken for a role refusal.
    expect(screen.queryByText(/can't complete that for your role/i)).toBeNull();
  });

  it("contract shape: citations fall back to answer_structured when the frame has none", async () => {
    const turn = await runTurn(CONTRACT_NEEDS_REVIEW_OUTPUT, false);
    expect(turn.citations?.map((c) => c.ref)).toEqual(["MEALS-PD"]);
    expect(screen.getByText(MEALS_TEXT)).toBeTruthy();
  });

  it("critic-rejected draft: no-answer message, the withheld draft is NOT rendered", async () => {
    const turn = await runTurn(REJECTED_NEEDS_REVIEW_OUTPUT, false);
    expect(screen.getByText(/No confident answer/i)).toBeTruthy();
    expect(screen.queryByText(REJECTED_TEXT)).toBeNull();
    expect(screen.queryByText(/ALCOHOL-X/)).toBeNull();
    expect(turn.citations ?? []).toEqual([]);
  });

  it("no draft: explicit no-confident-answer message, never a blank bubble", async () => {
    await runTurn(EMPTY_NEEDS_REVIEW_OUTPUT, false);
    expect(screen.getByText(/No confident answer/i)).toBeTruthy();
    expect(screen.getByText(/Needs review — low confidence/i)).toBeTruthy();
  });
});
