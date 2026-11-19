/**
 * UAT round 2 — planner layout below the xl (1280px) breakpoint.
 *
 * Browser evidence at 1222x686: after the first selection the draft-trip card
 * (PlannerTripStatus) appeared in the shrink-0 header, and the recommendations
 * pane — a `flex-1 overflow-hidden` item of a height-locked column — collapsed
 * to 0px (options list clientHeight 32 = its padding), so hotel cards could
 * not be seen or clicked. At ~610px height the composer covered a Select.
 *
 * jsdom cannot lay out a page, so these tests pin the STRUCTURAL contract
 * (the multi-width check is a headless-Chromium harness, see the commit):
 *   - below xl nothing in the planner is height-locked: the page scrolls
 *     naturally in <main>, the chat has a fixed usable height, and the
 *     recommendations keep a non-zero min-height and grow with their cards;
 *   - every height lock / inner scroll of the side-by-side layout is xl-only;
 *   - the options list leaves room under the last card for the fixed AI FAB;
 *   - the composer is in-flow (never fixed/sticky over the cards);
 *   - chat auto-scroll moves only the message list, never the page.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import * as React from "react";
import { render, cleanup, act } from "@testing-library/react";

vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { post: vi.fn(), get: vi.fn() },
}));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
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
  useSelectedOptionIds: () => new Set<string>(["DL333"]),
}));
// The real card needs a trip + session; the layout only cares where it sits.
vi.mock("@/components/planner/planner-trip-status", () => ({
  PlannerTripStatus: ({ className }: { className?: string }) => (
    <div data-testid="trip-status" className={className}>
      draft trip
    </div>
  ),
}));

import { PlannerScreen } from "@/components/planner/planner-screen";
import { usePlannerStore } from "@/stores/planner-store";
import type { ShortlistOption } from "@/lib/types";

function option(over: Partial<ShortlistOption>): ShortlistOption {
  return {
    rank: 1,
    option_id: "DL333",
    segment_type: "flight",
    supplier: "Delta Air Lines",
    amount_usd: 147,
    sustainability_score: 3,
    cabin_class: "economy",
    duration_hours: 2.5,
    is_direct: true,
    city: null,
    nightly_rate_usd: null,
    room_type: null,
    in_policy: true,
    policy_label: "In Policy",
    policy_reason: null,
    clause_refs: [],
    requires_approver: false,
    explanation: "",
    ...over,
  };
}

const SHORTLIST = [
  option({}),
  option({
    rank: 2,
    option_id: "JFK-HILTON",
    segment_type: "hotel",
    supplier: "Hilton",
    amount_usd: 218,
    city: "New York",
    nightly_rate_usd: 218,
    cabin_class: null,
    duration_hours: null,
    is_direct: null,
  }),
];

const scrollIntoView = vi.fn();
const scrollTo = vi.fn();

beforeEach(() => {
  usePlannerStore.getState().reset();
  sessionStorage.clear();
  scrollIntoView.mockReset();
  scrollTo.mockReset();
  Element.prototype.scrollIntoView = scrollIntoView;
  Element.prototype.scrollTo = scrollTo as never;
  usePlannerStore.setState({
    conversationId: "conv-1",
    selectedTripId: "t1",
    shortlist: SHORTLIST,
    turns: [
      {
        id: "u1",
        role: "user",
        content: "ORD to JFK Nov 17-20",
        timestamp: "2026-11-11T00:00:00Z",
      },
    ],
  });
});
afterEach(() => cleanup());

function region(container: HTMLElement, name: string): HTMLElement {
  const el = container.querySelector<HTMLElement>(
    `[data-planner-layout="${name}"]`,
  );
  expect(el, `region ${name}`).not.toBeNull();
  return el as HTMLElement;
}
function classes(el: Element): string[] {
  return (el.getAttribute("class") ?? "").split(/\s+/).filter(Boolean);
}
/** Classes that apply at EVERY width (no responsive / state prefix). */
function base(el: Element): string[] {
  return classes(el).filter((c) => !c.includes(":"));
}

const HEIGHT_LOCKS = ["h-full", "h-screen", "overflow-hidden", "flex-1"];

describe("PlannerScreen layout — column mode (below xl)", () => {
  it("does not lock the planner root or body to the viewport height", () => {
    const { container } = render(<PlannerScreen />);
    for (const name of ["root", "body"]) {
      const el = region(container, name);
      for (const lock of HEIGHT_LOCKS) {
        expect(base(el), `${name} has bare ${lock}`).not.toContain(lock);
      }
    }
    // The column stacks; the side-by-side row is xl-only.
    expect(base(region(container, "body"))).toContain("flex-col");
  });

  it("keeps the side-by-side height locks at xl", () => {
    const { container } = render(<PlannerScreen />);
    const root = classes(region(container, "root"));
    expect(root).toEqual(
      expect.arrayContaining(["xl:h-full", "xl:overflow-hidden"]),
    );
    const body = classes(region(container, "body"));
    expect(body).toEqual(
      expect.arrayContaining([
        "xl:flex-row",
        "xl:flex-1",
        "xl:min-h-0",
        "xl:overflow-hidden",
      ]),
    );
    const conv = classes(region(container, "conversation"));
    expect(conv).toEqual(
      expect.arrayContaining(["xl:h-auto", "xl:w-[40%]", "xl:min-h-0"]),
    );
    const recs = classes(region(container, "recommendations"));
    expect(recs).toEqual(
      expect.arrayContaining(["xl:flex-1", "xl:min-h-0", "xl:overflow-hidden"]),
    );
  });

  it("gives the chat a fixed usable height that cannot be squeezed", () => {
    const { container } = render(<PlannerScreen />);
    const conv = base(region(container, "conversation"));
    expect(conv).toContain("shrink-0");
    expect(conv.some((c) => /^h-\[\d+vh\]$/.test(c))).toBe(true);
    expect(conv.some((c) => /^min-h-\[\d+(\.\d+)?rem\]$/.test(c))).toBe(true);
  });

  it("never lets the recommendations collapse: non-zero min-height, no clip", () => {
    const { container } = render(<PlannerScreen />);
    const recs = base(region(container, "recommendations"));
    expect(recs.some((c) => /^min-h-\[\d+(\.\d+)?rem\]$/.test(c))).toBe(true);
    expect(recs).not.toContain("overflow-hidden");
    expect(recs).not.toContain("min-h-0");
  });

  it("renders the trip card in-flow above the body, not inside it", () => {
    const { container, getByTestId } = render(<PlannerScreen />);
    const card = getByTestId("trip-status");
    expect(region(container, "body").contains(card)).toBe(false);
    expect(region(container, "root").contains(card)).toBe(true);
  });

  it("the options list scrolls itself only at xl and clears the AI FAB", () => {
    const { container, getAllByRole } = render(<PlannerScreen />);
    const select = getAllByRole("button", { name: /^Select / })[0];
    const recs = region(container, "recommendations");
    const list = recs.firstElementChild as HTMLElement;
    expect(list.contains(select)).toBe(true);
    const own = base(list);
    // No height lock below xl: the list grows with its cards.
    expect(own).not.toContain("h-full");
    expect(own).not.toContain("min-h-0");
    // The fixed 56px FAB sits 24px above the viewport bottom: the last card's
    // Select must be able to scroll clear of it (>= 5rem bottom padding).
    expect(own).toContain("pb-20");
  });

  it("keeps the composer in-flow inside the chat (never fixed/sticky)", () => {
    const { container, getByRole } = render(<PlannerScreen />);
    const box = getByRole("textbox", { name: "Message input" });
    const conv = region(container, "conversation");
    expect(conv.contains(box)).toBe(true);
    for (
      let el: Element | null = box;
      el && el !== conv;
      el = el.parentElement
    ) {
      expect(base(el)).not.toContain("fixed");
      expect(base(el)).not.toContain("sticky");
    }
  });

  it("auto-scroll moves only the message list, never the page", () => {
    const { container } = render(<PlannerScreen />);
    scrollIntoView.mockReset();
    scrollTo.mockReset();
    act(() => {
      usePlannerStore.getState().addTurn({
        id: "a1",
        role: "assistant",
        content: "Here are your options.",
        timestamp: "2026-11-11T00:00:01Z",
      });
    });
    // scrollIntoView would also scroll <main> (column mode) and yank the user
    // away from the card they just clicked.
    expect(scrollIntoView).not.toHaveBeenCalled();
    const messages = region(container, "messages");
    expect(scrollTo).toHaveBeenCalled();
    expect(scrollTo.mock.contexts.at(-1)).toBe(messages);
  });
});
