import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  act,
  renderHook,
  waitFor,
} from "@testing-library/react";

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("@/lib/query/conversation-hooks", () => ({
  useConversation: () => ({ data: undefined }),
}));
vi.mock("@/lib/query/trip-hooks", () => ({
  useTrip: () => ({ data: undefined }),
  useTripSegments: () => ({ data: undefined }),
  tripQueryKeys: {},
}));
vi.mock("@/lib/planner/use-trip-selection", () => ({
  useTripSelection: () => ({
    selectOption: vi.fn(),
    selecting: false,
    selectError: null,
  }),
  useSelectedOptionIds: () => new Set<string>(),
}));

import { streamMessages, type StreamCallbacks } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import { usePlannerConversation } from "@/lib/planner/use-planner-conversation";
import { PlannerScreen } from "@/components/planner/planner-screen";
import { usePlannerStore } from "@/stores/planner-store";
import {
  CONTRACT_NEEDS_REVIEW_OUTPUT,
  EMPTY_NEEDS_REVIEW_OUTPUT,
  REJECTED_NEEDS_REVIEW_OUTPUT,
  REJECTED_TEXT,
  MEALS_TEXT,
  SSE_DONE,
  SSE_STATUS,
  sseResult,
} from "./needs-review-fixtures";

const stream = vi.mocked(streamMessages);
const post = vi.mocked(api.post);

/** Emit frames ASYNCHRONOUSLY (like a real network stream) — the old hook
 *  resolved sendMessage before any of these arrived. */
function asyncStream(frames: Array<[string, string]>) {
  stream.mockImplementation(
    (_id: string, _body: unknown, cbs: StreamCallbacks) => {
      let aborted = false;
      void (async () => {
        for (const [ev, data] of frames) {
          await new Promise((r) => setTimeout(r, 5));
          if (aborted) return;
          cbs.onEvent?.(ev, data);
        }
        await new Promise((r) => setTimeout(r, 5));
        if (!aborted) cbs.onComplete();
      })();
      return () => {
        aborted = true;
      };
    },
  );
}

const GROUNDED = JSON.stringify({
  output: {
    answer_type: "grounded",
    answer: "The nightly cap is USD 250 [Policy §HOTEL-CAP].",
    asserted: true,
    critique: null,
  },
  citations: [{ kind: "clause_ref", ref: "HOTEL-CAP" }],
});

beforeEach(() => {
  usePlannerStore.getState().reset();
  sessionStorage.clear();
  stream.mockReset();
  post.mockReset();
  post.mockResolvedValue({ id: "conv-policy" } as never);
  // jsdom lacks scrollIntoView (ConversationPanel auto-scroll).
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(cleanup);

describe("C7-07 usePlannerConversation", () => {
  it("sendMessage resolves AFTER the stream with the final policy answer", async () => {
    asyncStream([
      ["status", SSE_STATUS],
      ["result", sseResult(CONTRACT_NEEDS_REVIEW_OUTPUT)],
      ["done", SSE_DONE],
    ]);
    const { result } = renderHook(() => usePlannerConversation(null));
    let res: Awaited<ReturnType<typeof result.current.sendMessage>> | undefined;
    await act(async () => {
      res = await result.current.sendMessage(
        { content: "meals?" },
        "policy_qa",
      );
    });
    expect(res?.cancelled).toBe(false);
    expect(res?.done).toBe(true);
    expect(res?.policyAnswer?.answerType).toBe("needs_review");
    expect(res?.policyAnswer?.answer).toBe(MEALS_TEXT);
    expect(res?.policyAnswer?.citations[0]?.ref).toBe("MEALS-PD");
    // Hook state agrees with the resolved value.
    expect(result.current.state.policyAnswer?.answer).toBe(MEALS_TEXT);
  });

  it("a superseded stream resolves as cancelled (no stale turn)", async () => {
    asyncStream([["result", GROUNDED]]);
    const { result } = renderHook(() => usePlannerConversation(null));
    let first: Promise<{ cancelled: boolean }> | undefined;
    await act(async () => {
      first = result.current.sendMessage({ content: "a" }, "policy_qa");
      await new Promise((r) => setTimeout(r, 0));
      result.current.cancel();
    });
    await expect(first).resolves.toMatchObject({ cancelled: true });
  });
});

async function askPolicy(question: string) {
  render(<PlannerScreen />);
  fireEvent.click(screen.getByRole("button", { name: /Ask Policy/i }));
  const box = screen.getByRole("textbox");
  fireEvent.change(box, { target: { value: question } });
  fireEvent.keyDown(box, { key: "Enter" });
}

describe("C7-07 PlannerScreen Ask Policy", () => {
  it("renders a needs_review answer text + citation + caution label, never the placeholder", async () => {
    asyncStream([
      ["status", SSE_STATUS],
      ["result", sseResult(CONTRACT_NEEDS_REVIEW_OUTPUT)],
      ["done", SSE_DONE],
    ]);
    await askPolicy("What is the daily meal per diem?");
    await waitFor(() => expect(screen.getByText(MEALS_TEXT)).toBeTruthy());
    expect(screen.getByText(/Needs review — low confidence/i)).toBeTruthy();
    expect(screen.getAllByText(/MEALS-PD/).length).toBeGreaterThan(0);
    expect(screen.queryByText("Policy response received.")).toBeNull();
  });

  it("renders a grounded answer", async () => {
    asyncStream([
      ["result", GROUNDED],
      ["done", SSE_DONE],
    ]);
    await askPolicy("What is the nightly hotel cap?");
    await waitFor(() =>
      expect(
        screen.getByText("The nightly cap is USD 250 [Policy §HOTEL-CAP]."),
      ).toBeTruthy(),
    );
    expect(screen.queryByText("Policy response received.")).toBeNull();
    expect(screen.queryByText("true")).toBeNull();
  });

  it("renders an abstention", async () => {
    asyncStream([
      [
        "result",
        JSON.stringify({
          output: {
            answer_type: "silent",
            answer: null,
            rationale: "This query is not covered by the Atlas T&E documents.",
            reason: "below_threshold",
            escalation: { message: "For guidance, contact finance@atlas." },
            near_topic: null,
          },
          citations: [],
        }),
      ],
      ["done", SSE_DONE],
    ]);
    await askPolicy("Can I expense a pony?");
    await waitFor(() =>
      expect(screen.getByText(/Policy is silent on this/i)).toBeTruthy(),
    );
    expect(screen.getByText(/contact finance@atlas/i)).toBeTruthy();
    expect(screen.queryByText("Policy response received.")).toBeNull();
  });

  it("renders a clarify question", async () => {
    asyncStream([
      [
        "result",
        JSON.stringify({
          output: { clarify: true, question: "Domestic or international?" },
          citations: [],
        }),
      ],
      ["done", SSE_DONE],
    ]);
    await askPolicy("What is the per diem?");
    await waitFor(() =>
      expect(
        screen.getAllByText("Domestic or international?").length,
      ).toBeGreaterThan(0),
    );
  });

  it("needs_review with no draft shows the explicit no-answer message", async () => {
    asyncStream([
      ["result", sseResult(EMPTY_NEEDS_REVIEW_OUTPUT, [])],
      ["done", SSE_DONE],
    ]);
    await askPolicy("Hotel cap?");
    await waitFor(() =>
      expect(screen.getByText(/No confident answer/i)).toBeTruthy(),
    );
    expect(screen.queryByText("Policy response received.")).toBeNull();
  });

  it("critic-rejected draft: shows no-answer message, NEVER the withheld draft", async () => {
    asyncStream([
      ["result", sseResult(REJECTED_NEEDS_REVIEW_OUTPUT, [])],
      ["done", SSE_DONE],
    ]);
    await askPolicy("Is alcohol reimbursable?");
    await waitFor(() =>
      expect(screen.getByText(/No confident answer/i)).toBeTruthy(),
    );
    expect(screen.queryByText(REJECTED_TEXT)).toBeNull();
    expect(screen.queryByText(/Alcohol is fully reimbursable/)).toBeNull();
    expect(screen.queryByText(/ALCOHOL-X/)).toBeNull();
  });
});
