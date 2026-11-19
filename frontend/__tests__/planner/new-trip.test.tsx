/**
 * UAT-2 Issue 4 — "New trip" starts a fresh planning conversation.
 *
 * One planning conversation saves one draft trip, so planning a second,
 * separate trip needs a fresh conversation: cleared chips / history, a new
 * conversation id, and the old ?c= id never re-adopted while the URL catches up.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  render,
  screen,
  cleanup,
  fireEvent,
  waitFor,
} from "@testing-library/react";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn() }),
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
import { PlannerScreen } from "@/components/planner/planner-screen";
import { usePlannerStore } from "@/stores/planner-store";
import { EMPTY_INTENT } from "@/lib/planner/intent";

const post = vi.mocked(api.post);
const stream = vi.mocked(streamMessages);

beforeEach(() => {
  nav.search = "c=conv-old";
  nav.replace.mockReset();
  usePlannerStore.getState().reset();
  sessionStorage.clear();
  post.mockReset();
  stream.mockReset();
  post.mockResolvedValue({ id: "conv-new" } as never);
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => cleanup());

describe("PlannerScreen — New trip", () => {
  it("clears the plan and the next message opens a NEW conversation", async () => {
    usePlannerStore.setState({
      conversationId: "conv-old",
      selectedTripId: "trip-old",
      intent: { ...EMPTY_INTENT, origin: "ORD", destination: "JFK" },
      turns: [
        {
          id: "t1",
          role: "user",
          content: "Fly to New York",
          timestamp: "2026-11-11T00:00:00Z",
        },
      ],
    });
    render(<PlannerScreen />);

    fireEvent.click(screen.getByRole("button", { name: /New trip/ }));

    const s = usePlannerStore.getState();
    expect(s.conversationId).toBeNull();
    expect(s.selectedTripId).toBeNull();
    expect(s.turns).toEqual([]);
    expect(s.intent).toEqual(EMPTY_INTENT);
    expect(nav.replace).toHaveBeenCalledWith("/planner", { scroll: false });

    // The URL still says ?c=conv-old (router not caught up): it must not be
    // re-adopted — the next message creates a fresh conversation.
    stream.mockImplementationOnce(
      (_id: string, _body: unknown, cbs: StreamCallbacks) => {
        cbs.onComplete();
        return () => {};
      },
    );
    const box = screen.getByRole("textbox", { name: "Message input" });
    fireEvent.change(box, { target: { value: "Fly to Boston" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(stream).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith("/conversations", {
      body: { context_type: "travel_planning" },
    });
    expect(stream.mock.calls[0][0]).toBe("conv-new");
  });

  it("is not offered before anything was planned", () => {
    nav.search = "";
    render(<PlannerScreen />);
    expect(screen.queryByRole("button", { name: /New trip/ })).toBeNull();
  });
});
