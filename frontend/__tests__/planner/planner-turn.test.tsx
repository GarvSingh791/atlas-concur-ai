import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  cleanup,
  renderHook,
  act,
  fireEvent,
  waitFor,
} from "@testing-library/react";

// Controllable ?c= search param (reload scenario).
const nav = vi.hoisted(() => ({ search: "" }));

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
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
import type { PlannerSendResult } from "@/lib/planner/use-planner-conversation";
import {
  applyPlannerTurn,
  assistantTextForTurn,
  NO_OPTIONS_FALLBACK,
} from "@/lib/planner/apply-turn";
import { usePlannerStore } from "@/stores/planner-store";
import { RecommendationPanel } from "@/components/planner/recommendation-panel";
import { PlannerScreen } from "@/components/planner/planner-screen";

const post = vi.mocked(api.post);
const stream = vi.mocked(streamMessages);

const ORIGIN_Q =
  "What city or airport are you departing from? (e.g. SFO, London)";

/** Queue one turn's frames; emitted asynchronously like a real stream. */
function queueTurn(output: Record<string, unknown>) {
  stream.mockImplementationOnce(
    (_id: string, _body: unknown, cbs: StreamCallbacks) => {
      void (async () => {
        await new Promise((r) => setTimeout(r, 1));
        cbs.onEvent?.(
          "status",
          JSON.stringify({
            task_type: "trip_planning",
            route: "llm",
            status: "ok",
          }),
        );
        cbs.onEvent?.("result", JSON.stringify({ output, citations: [] }));
        cbs.onEvent?.(
          "done",
          JSON.stringify({ needs_review: false, message_id: "m", sequence: 2 }),
        );
        cbs.onComplete();
      })();
      return () => {};
    },
  );
}

function result(over: Partial<PlannerSendResult>): PlannerSendResult {
  return {
    conversationId: "conv-1",
    streaming: false,
    streamingText: "",
    shortlist: [],
    citations: [],
    clarifyQuestion: null,
    shortlistReturned: false,
    message: null,
    intent: null,
    tripId: null,
    policyAnswer: null,
    resultMessage: null,
    done: true,
    error: null,
    cancelled: false,
    ...over,
  };
}

beforeEach(() => {
  nav.search = "";
  usePlannerStore.getState().reset();
  sessionStorage.clear();
  post.mockReset();
  stream.mockReset();
  post.mockResolvedValue({ id: "conv-new" } as never);
  // jsdom has no layout APIs; the conversation panel auto-scrolls.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(cleanup);

describe("usePlannerConversation", () => {
  it("resolves each turn with its own intent / shortlist flag", async () => {
    const { result: hook } = renderHook(() => usePlannerConversation("c-1"));

    queueTurn({
      clarify: true,
      question: ORIGIN_Q,
      intent: { origin: null, destination: "JFK" },
    });
    let first: PlannerSendResult | undefined;
    await act(async () => {
      first = await hook.current.sendMessage({ content: "to NYC next Tue" });
    });
    expect(first?.clarifyQuestion).toBe(ORIGIN_Q);
    expect(first?.intent?.destination).toBe("JFK");
    expect(first?.shortlistReturned).toBe(false);

    queueTurn({
      clarify: false,
      shortlist: [],
      trip_id: null,
      reflection_outcome: null,
      degraded: false,
      message: "No options found (ORD → JFK, 2026-11-17).",
      intent: { origin: "ORD", destination: "JFK" },
    });
    let second: PlannerSendResult | undefined;
    await act(async () => {
      second = await hook.current.sendMessage({ content: "Chicago (ORD)" });
    });
    expect(second?.clarifyQuestion).toBeNull();
    expect(second?.shortlistReturned).toBe(true);
    expect(second?.message).toContain("No options found");
    expect(second?.intent?.origin).toBe("ORD");
    expect(post).not.toHaveBeenCalled();
  });

  it("adopts a conversation id that arrives after mount (reload with ?c=)", async () => {
    const { result: hook, rerender } = renderHook(
      ({ id }: { id: string | null }) => usePlannerConversation(id),
      { initialProps: { id: null as string | null } },
    );
    rerender({ id: "conv-from-url" });

    queueTurn({ clarify: true, question: ORIGIN_Q });
    await act(async () => {
      await hook.current.sendMessage({ content: "hello" });
    });

    // Continues the URL's conversation instead of silently creating a new one.
    expect(post).not.toHaveBeenCalled();
    expect(stream.mock.calls[0][0]).toBe("conv-from-url");
  });
});

describe("assistantTextForTurn / applyPlannerTurn", () => {
  it("a clarify turn yields exactly the question and fills chips from intent", () => {
    const turn = result({
      clarifyQuestion: ORIGIN_Q,
      intent: { destination: "JFK", return_date: "2026-11-19" },
    });
    expect(assistantTextForTurn(turn)).toBe(ORIGIN_Q);

    applyPlannerTurn(turn, usePlannerStore.getState());
    const after = usePlannerStore.getState();
    expect(after.intent.destination).toBe("JFK");
    expect(after.intent.return_date).toBe("2026-11-19");
    expect(after.noOptionsMessage).toBeNull();
  });

  it("an empty shortlist surfaces the server message and clears old options", () => {
    usePlannerStore
      .getState()
      .setShortlist([{ option_id: "old" } as never], []);
    const turn = result({
      shortlistReturned: true,
      shortlist: [],
      message: "No options found matching your request (ORD → BOS).",
    });
    expect(assistantTextForTurn(turn)).toContain("ORD → BOS");
    applyPlannerTurn(turn, usePlannerStore.getState());
    const after = usePlannerStore.getState();
    expect(after.shortlist).toEqual([]);
    expect(after.noOptionsMessage).toContain("ORD → BOS");
  });

  it("falls back to a generic no-options text when the server sent none", () => {
    const turn = result({ shortlistReturned: true, shortlist: [] });
    expect(assistantTextForTurn(turn)).toBe(NO_OPTIONS_FALLBACK);
  });

  it("errors produce an error turn and leave the store untouched", () => {
    const turn = result({ error: "boom", intent: { origin: "SFO" } });
    expect(assistantTextForTurn(turn)).toBe("Error: boom");
    applyPlannerTurn(turn, usePlannerStore.getState());
    expect(usePlannerStore.getState().intent.origin).toBeNull();
  });
});

describe("RecommendationPanel empty state", () => {
  it("shows the backend's no-options message when the search was empty", () => {
    usePlannerStore
      .getState()
      .setNoOptionsMessage("No options found (ORD → BOS, 2026-11-17).");
    render(<RecommendationPanel onSelectOption={() => {}} />);
    expect(screen.getByText("No options found")).toBeTruthy();
    expect(screen.getByText(/ORD → BOS/)).toBeTruthy();
    expect(screen.queryByText("No options yet")).toBeNull();
  });

  it("keeps the generic prompt before any search", () => {
    render(<RecommendationPanel onSelectOption={() => {}} />);
    expect(screen.getByText("No options yet")).toBeTruthy();
  });
});

function typeMessage(text: string) {
  const box = screen.getByRole("textbox", { name: "Message input" });
  fireEvent.change(box, { target: { value: text } });
  fireEvent.keyDown(box, { key: "Enter" });
}

describe("PlannerScreen (plan mode)", () => {
  it("fills chips from the message and shows the clarify question once", async () => {
    queueTurn({
      clarify: true,
      question: ORIGIN_Q,
      intent: {
        origin: null,
        destination: "JFK",
        departure_date: "2026-11-17",
        return_date: "2026-11-19",
        travelers: 1,
        currency: "USD",
        purpose: "Client meeting",
      },
    });
    render(<PlannerScreen />);
    typeMessage("Fly to New York next Tuesday for 3 days, client meeting");

    await waitFor(() =>
      expect(usePlannerStore.getState().intent.destination).toBe("JFK"),
    );
    await waitFor(() => expect(screen.getAllByText(ORIGIN_Q)).toHaveLength(1));
    const s = usePlannerStore.getState();
    expect(s.intent.return_date).toBe("2026-11-19");
    expect(s.intent.purpose).toBe("Client meeting");
    expect(screen.getByText("Client meeting")).toBeTruthy(); // Purpose chip

    // The follow-up answer is sent with the merged chips.
    queueTurn({
      clarify: false,
      shortlist: [],
      trip_id: null,
      reflection_outcome: "passed",
      degraded: false,
      message: "No options found matching your request (ORD → JFK).",
      intent: { origin: "ORD" },
    });
    typeMessage("Chicago (ORD)");
    await waitFor(() =>
      expect(usePlannerStore.getState().intent.origin).toBe("ORD"),
    );
    const body = stream.mock.calls[1][1] as {
      payload: Record<string, unknown>;
    };
    expect(body.payload.destination).toBe("JFK");
    expect(body.payload.return_date).toBe("2026-11-19");
    await waitFor(() =>
      expect(screen.getAllByText(/ORD → JFK/).length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText(ORIGIN_Q)).toHaveLength(1);
  });

  it("after a reload with ?c=, a new message continues that conversation", async () => {
    nav.search = "c=conv-existing";
    queueTurn({ clarify: true, question: ORIGIN_Q });
    render(<PlannerScreen />);
    typeMessage("hello again");
    await waitFor(() => expect(stream).toHaveBeenCalledTimes(1));
    expect(stream.mock.calls[0][0]).toBe("conv-existing");
    expect(post).not.toHaveBeenCalled();
  });
});
