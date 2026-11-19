import { describe, it, expect, beforeEach, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));

import { streamMessages } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import { useAssistantConversation } from "@/lib/assistant/use-assistant-conversation";
import { useAssistantStore } from "@/stores/assistant-store";

const post = vi.mocked(api.post);
const get = vi.mocked(api.get);
const stream = vi.mocked(streamMessages);

const STATUS = JSON.stringify({
  task_type: "policy_qa",
  route: "llm",
  confidence: 0.88,
  status: "ok",
});
const RESULT = JSON.stringify({
  output: {
    answer_type: "grounded",
    answer: "Yes.",
    asserted: "§4.1",
    critique: null,
  },
  citations: [{ kind: "clause_ref", ref: "§4.1" }],
});
const DONE = JSON.stringify({
  needs_review: false,
  message_id: "msg-1",
  sequence: 2,
});

beforeEach(() => {
  useAssistantStore.getState().reset();
  sessionStorage.clear();
  post.mockReset();
  get.mockReset();
  stream.mockReset();
  post.mockResolvedValue({ id: "conv-1" } as never);
  get.mockResolvedValue({
    id: "conv-1",
    context_type: "expense",
    context_ref_id: "rep-1",
    created_at: "",
    messages: [
      {
        id: "msg-1",
        role: "assistant",
        content: "Yes.",
        tool_calls: { tools_used: { PolicyEvalTool: true } },
        citations: { citations: [{ kind: "clause_ref", ref: "§4.1" }] },
        sequence: 2,
        created_at: "",
      },
    ],
  } as never);
});

describe("useAssistantConversation", () => {
  it("creates a context-scoped conversation and renders a grounded turn", async () => {
    useAssistantStore
      .getState()
      .openWithContext({ contextType: "expense", contextRefId: "rep-1" });

    stream.mockImplementation((_id, _body, cbs) => {
      cbs.onEvent?.("status", STATUS);
      cbs.onEvent?.("result", RESULT);
      cbs.onEvent?.("done", DONE);
      cbs.onComplete();
      return () => {};
    });

    const { result } = renderHook(() => useAssistantConversation());
    await act(async () => {
      await result.current.sendMessage("Is business class allowed?");
    });

    // Context-scoped create (validate gate G2)
    expect(post).toHaveBeenCalledWith("/conversations", {
      body: { context_type: "expense", context_ref_id: "rep-1" },
    });
    // Stream sent with explain_with_llm for narrative streaming
    expect(stream).toHaveBeenCalledWith(
      "conv-1",
      { content: "Is business class allowed?", explain_with_llm: true },
      expect.anything(),
    );

    const turns = useAssistantStore.getState().turns;
    expect(turns.map((t) => t.role)).toEqual(["user", "assistant"]);
    const a = turns[1];
    expect(a.answer?.answerType).toBe("grounded");
    expect(a.answer?.answer).toBe("Yes.");
    expect(a.confidence).toBe(88);
    expect(a.route).toBe("llm");
    expect(a.citations).toHaveLength(1);
    expect(a.isStreaming).toBe(false);

    // Tool activity backfilled from the persisted trace (GET after done)
    await waitFor(() => {
      const last = useAssistantStore.getState().turns[1];
      expect(last.toolActivity?.[0]?.name).toBe("Policy Evaluation");
    });
  });

  it("renders 'policy is silent' from the result frame", async () => {
    stream.mockImplementation((_id, _body, cbs) => {
      cbs.onEvent?.(
        "status",
        JSON.stringify({
          task_type: "policy_qa",
          route: "deterministic",
          confidence: 0.99,
          status: "ok",
        }),
      );
      cbs.onEvent?.(
        "result",
        JSON.stringify({
          output: {
            answer_type: "silent",
            reason: "No matching clause",
            answer: null,
          },
          citations: [],
        }),
      );
      cbs.onEvent?.("done", DONE);
      cbs.onComplete();
      return () => {};
    });

    const { result } = renderHook(() => useAssistantConversation());
    await act(async () => {
      await result.current.sendMessage("Can I expense a yacht?");
    });

    const a = useAssistantStore.getState().turns[1];
    expect(a.answer?.answerType).toBe("silent");
    expect(a.answer?.reason).toBe("No matching clause");
  });

  it("ignores malformed frames without corrupting the turn", async () => {
    stream.mockImplementation((_id, _body, cbs) => {
      cbs.onEvent?.("status", "{not json");
      cbs.onEvent?.("result", RESULT);
      cbs.onEvent?.("done", DONE);
      cbs.onComplete();
      return () => {};
    });

    const { result } = renderHook(() => useAssistantConversation());
    await act(async () => {
      await result.current.sendMessage("hi");
    });

    const a = useAssistantStore.getState().turns[1];
    expect(a.answer?.answerType).toBe("grounded");
    expect(a.error).toBeFalsy();
  });

  it("fails closed on mid-stream abort and offers retry", async () => {
    const cancelFn = vi.fn();
    stream.mockImplementation((_id, _body, cbs) => {
      cbs.onEvent?.("status", STATUS); // in-flight: no result/done/complete
      return cancelFn;
    });

    const { result } = renderHook(() => useAssistantConversation());
    await act(async () => {
      await result.current.sendMessage("slow question");
    });

    expect(useAssistantStore.getState().streaming).toBe(true);

    act(() => result.current.cancel());

    expect(cancelFn).toHaveBeenCalled();
    const a = useAssistantStore.getState().turns[1];
    expect(a.isStreaming).toBe(false);
    expect(a.error).toBe("Response interrupted.");
    expect(useAssistantStore.getState().streaming).toBe(false);
  });

  it("surfaces a transport error on the turn", async () => {
    stream.mockImplementation((_id, _body, cbs) => {
      cbs.onError(new Error("Stream failed: HTTP 500"));
      return () => {};
    });

    const { result } = renderHook(() => useAssistantConversation());
    await act(async () => {
      await result.current.sendMessage("boom");
    });

    const a = useAssistantStore.getState().turns[1];
    expect(a.error).toMatch(/Stream failed/);
    expect(a.isStreaming).toBe(false);
  });
});
