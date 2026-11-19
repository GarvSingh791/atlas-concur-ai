/**
 * UAT-2 Issue 4 — the planner's flight + hotel selections make ONE trip.
 *
 * Server side, one planning conversation holds one draft trip; these tests pin
 * the UI around it:
 *   * the result frame's trip_change / trip_notice are parsed defensively;
 *   * the toast says what happened (added / replaced / a NEW draft started),
 *     a re-pick of the same option is silent, a failure is a toast only;
 *   * a second click while a selection is saving is ignored;
 *   * a card the server did not save never stays "Selected";
 *   * BOTH the flight and the hotel on the trip show "Selected";
 *   * the trip status lists every saved leg + the estimated total;
 *   * My Trips cards are titled route + dates + purpose and open to the legs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  cleanup,
  renderHook,
  act,
  fireEvent,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { showToast, get } = vi.hoisted(() => ({
  showToast: vi.fn(),
  get: vi.fn(),
}));
// UAT2 Issue 7: owner actions (Submit) render only for the trip's traveler ("emp").
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => ({ user: { id: "emp" } }),
}));
vi.mock("@/lib/api/stream", () => ({ streamMessages: vi.fn() }));
vi.mock("@/lib/api/client", () => ({
  api: { get, post: vi.fn(), patch: vi.fn() },
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));

import { streamMessages, type StreamCallbacks } from "@/lib/api/stream";
import { discriminateOutput, parseTripChange } from "@/lib/planner/sse-events";
import {
  selectionContent,
  selectionToast,
  useTripSelection,
} from "@/lib/planner/use-trip-selection";
import { savedOptionIds } from "@/lib/planner/selected-options";
import { formatUsd, segmentLine, tripTitle } from "@/lib/trips/summary";
import { RecommendationPanel } from "@/components/planner/recommendation-panel";
import { PlannerTripStatus } from "@/components/planner/planner-trip-status";
import { TripList } from "@/components/trips/trip-list";
import { usePlannerStore } from "@/stores/planner-store";
import { EMPTY_INTENT } from "@/lib/planner/intent";
import type {
  PlannerIntent,
  ShortlistOption,
  Trip,
  TripChange,
  TripSegment,
} from "@/lib/types";

const stream = vi.mocked(streamMessages);

const INTENT: PlannerIntent = {
  ...EMPTY_INTENT,
  origin: "ORD",
  destination: "JFK",
  departure_date: "2026-11-17",
  return_date: "2026-11-20",
  travelers: 1,
  currency: "USD",
  purpose: "Client meeting",
};

function trip(over: Partial<Trip> = {}): Trip {
  return {
    id: "t1",
    organization_id: "org",
    traveler_id: "emp",
    origin: "ORD",
    primary_destination: "JFK",
    start_date: "2026-11-17",
    end_date: "2026-11-20",
    purpose: "Client meeting",
    estimated_cost: "801.00",
    budget_amount: null,
    currency: "USD",
    status: "draft",
    recommendation_context: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
    version: 2,
    approval_status: null,
    approval_reason: null,
    ...over,
  };
}

function segment(over: Partial<TripSegment>): TripSegment {
  return {
    id: "s1",
    trip_id: "t1",
    segment_type: "flight",
    supplier: "Delta Air Lines",
    start_datetime: null,
    end_datetime: null,
    location: null,
    amount: "147.00",
    currency: "USD",
    in_policy: true,
    policy_notes: null,
    details: { option_id: "DL333" },
    created_at: "2026-11-11T00:00:00Z",
    updated_at: "2026-11-11T00:00:00Z",
    ...over,
  };
}

const FLIGHT_SEG = segment({});
const HOTEL_SEG = segment({
  id: "s2",
  segment_type: "hotel",
  supplier: "Hilton",
  amount: "654.00",
  details: { option_id: "JFK-HILTON", nights: 3, nightly_rate_usd: 218 },
});

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
    option_id: "UA9",
    supplier: "United Airlines",
    amount_usd: 162,
  }),
  option({
    rank: 3,
    option_id: "JFK-HILTON",
    segment_type: "hotel",
    supplier: "Hilton",
    amount_usd: 218,
    city: "New York",
    nightly_rate_usd: 218,
    nights: 3,
    stay_total_usd: 654,
    cabin_class: null,
    duration_hours: null,
    is_direct: null,
  }),
];

function client() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
}

function wrap(ui: React.ReactElement, qc = client()) {
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function routeGet(map: Record<string, unknown>) {
  get.mockImplementation((path: string) => {
    if (path in map) return Promise.resolve(map[path]);
    return Promise.reject(new Error(`unexpected GET ${path}`));
  });
}

beforeEach(() => {
  showToast.mockReset();
  get.mockReset();
  stream.mockReset();
  usePlannerStore.getState().reset();
});
afterEach(() => cleanup());

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe("trip_change parsing", () => {
  it("parses a valid trip_change and the selection fields", () => {
    const out = discriminateOutput({
      clarify: false,
      shortlist: [],
      trip_id: "t1",
      trip_change: {
        action: "updated",
        segment_types_added: ["hotel"],
        segment_types_replaced: [],
        previous_trip: null,
        estimated_cost: "801.00",
        segment_count: 2,
      },
      trip_message: "Added the Hilton hotel.",
      trip_notice: null,
    });
    expect(out && "clarify" in out && !out.clarify && out.trip_change).toEqual({
      action: "updated",
      segment_types_added: ["hotel"],
      segment_types_replaced: [],
      previous_trip: null,
      estimated_cost: "801.00",
      segment_count: 2,
      end_date_change: null,
      stale_hotel: false,
    });
    expect(out && "trip_message" in out && out.trip_message).toBe(
      "Added the Hilton hotel.",
    );
  });

  it("UAT2 defect E: parses a return-date change made in place", () => {
    const tc = parseTripChange({
      action: "updated",
      segment_types_added: [],
      segment_types_replaced: ["hotel"],
      previous_trip: null,
      estimated_cost: "1019.00",
      segment_count: 2,
      end_date_change: { from: "2026-11-20", to: "2026-11-21" },
      stale_hotel: false,
    });
    expect(tc?.end_date_change).toEqual({
      from: "2026-11-20",
      to: "2026-11-21",
    });
    expect(tc?.stale_hotel).toBe(false);
    // Malformed -> absent, never a half-parsed object.
    const bad = parseTripChange({
      action: "updated",
      end_date_change: { from: 3 },
      stale_hotel: "yes",
    });
    expect(bad?.end_date_change ?? null).toBeNull();
    expect(bad?.stale_hotel ?? false).toBe(false);
  });

  it("an unknown action or a malformed value is null", () => {
    expect(parseTripChange({ action: "merged" })).toBeNull();
    expect(parseTripChange("updated")).toBeNull();
    expect(parseTripChange(null)).toBeNull();
  });
});

describe("selectionToast", () => {
  const base: TripChange = {
    action: "created",
    segment_types_added: ["flight"],
    segment_types_replaced: [],
    previous_trip: null,
    estimated_cost: "147.00",
    segment_count: 1,
  };

  it("created keeps the 'Trip saved' title", () => {
    expect(selectionToast(base, null)?.title).toBe("Trip saved as a draft");
  });

  it("hotel added names the leg and the new total", () => {
    const t = selectionToast(
      {
        ...base,
        action: "updated",
        segment_types_added: ["hotel"],
        estimated_cost: "801.00",
      },
      null,
    );
    expect(t).toMatchObject({
      variant: "success",
      title: "Trip saved — hotel added",
      description: "Estimated cost $801.00.",
    });
  });

  it("flight replaced", () => {
    expect(
      selectionToast(
        {
          ...base,
          action: "updated",
          segment_types_added: [],
          segment_types_replaced: ["flight"],
        },
        null,
      )?.title,
    ).toBe("Trip saved — flight replaced");
  });

  it("UAT2 defect E: a return-date change says the same draft was updated", () => {
    expect(
      selectionToast(
        {
          ...base,
          action: "updated",
          segment_types_added: [],
          segment_types_replaced: ["hotel"],
          estimated_cost: "1019.00",
          end_date_change: { from: "2026-11-20", to: "2026-11-21" },
          stale_hotel: false,
        },
        null,
      ),
    ).toEqual({
      variant: "success",
      title: "Trip saved — return date updated",
      description: "Return date moved to 2026-11-21. Estimated cost $1,019.00.",
    });
  });

  it("UAT2 defect E: a hotel left on the old dates is flagged", () => {
    expect(
      selectionToast(
        {
          ...base,
          action: "updated",
          segment_types_added: [],
          end_date_change: { from: "2026-11-20", to: "2026-11-21" },
          stale_hotel: true,
        },
        null,
      ),
    ).toEqual({
      variant: "info",
      title: "Trip saved — return date updated",
      description:
        "Return date moved to 2026-11-21. The hotel still covers the old dates — select a hotel to cover the new stay.",
    });
  });

  it("re-picking the same option is silent", () => {
    expect(selectionToast({ ...base, action: "unchanged" }, null)).toBeNull();
  });

  it("a new draft started instead shows the server's notice", () => {
    const notice =
      "Started a new draft trip ORD → BOS, 2026-12-03 – 2026-12-05 — your earlier draft ORD → JFK is still in Trips.";
    expect(selectionToast(base, notice)).toEqual({
      variant: "info",
      title: "Trip saved — started a new draft trip",
      description: notice,
    });
  });
});

describe("trip summary helpers", () => {
  it("title = route + dates + purpose", () => {
    expect(tripTitle(trip())).toBe(
      "ORD → JFK · 2026-11-17 – 2026-11-20 · Client meeting",
    );
    expect(tripTitle(trip({ end_date: "2026-11-17", purpose: null }))).toBe(
      "ORD → JFK · 2026-11-17",
    );
  });

  it("segment lines carry nights and the stay total", () => {
    expect(segmentLine(FLIGHT_SEG)).toBe("Flight · Delta Air Lines · $147.00");
    expect(segmentLine(HOTEL_SEG)).toBe("Hotel · Hilton · 3 nights · $654.00");
    expect(formatUsd("1234.5")).toBe("$1,234.50");
    expect(formatUsd(null)).toBeNull();
  });

  it("selection message names what was picked", () => {
    expect(selectionContent(SHORTLIST[2])).toBe("Select Hilton hotel");
  });
});

describe("savedOptionIds", () => {
  it("both legs of a matching trip are selected", () => {
    const ids = savedOptionIds(trip(), [FLIGHT_SEG, HOTEL_SEG], INTENT);
    expect(Array.from(ids).sort()).toEqual(["DL333", "JFK-HILTON"]);
  });

  it("nothing is selected once the request names other dates or a route", () => {
    expect(
      savedOptionIds(trip(), [FLIGHT_SEG, HOTEL_SEG], {
        ...INTENT,
        return_date: "2026-11-21",
      }).size,
    ).toBe(0);
    expect(
      savedOptionIds(trip(), [FLIGHT_SEG], { ...INTENT, destination: "BOS" })
        .size,
    ).toBe(0);
  });

  it("a cancelled trip selects nothing", () => {
    expect(
      savedOptionIds(trip({ status: "cancelled" }), [FLIGHT_SEG], INTENT).size,
    ).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// useTripSelection
// ---------------------------------------------------------------------------

function seedStore() {
  usePlannerStore.setState({
    conversationId: "conv-1",
    intent: INTENT,
    shortlist: SHORTLIST,
  });
}

/** A stream the test settles by hand. */
function heldStream() {
  const held: { cbs?: StreamCallbacks; body?: unknown } = {};
  stream.mockImplementation((_id, body, cbs) => {
    held.cbs = cbs;
    held.body = body;
    return () => {};
  });
  return held;
}

function emitResult(cbs: StreamCallbacks, output: Record<string, unknown>) {
  cbs.onEvent?.("result", JSON.stringify({ output, citations: [] }));
  cbs.onEvent?.(
    "done",
    JSON.stringify({ needs_review: false, message_id: "m", sequence: 2 }),
  );
}

describe("useTripSelection", () => {
  it("ignores a second click while the first selection is saving", async () => {
    seedStore();
    const held = heldStream();
    const qc = client();
    const { result } = renderHook(() => useTripSelection(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={qc}>{children}</QueryClientProvider>
      ),
    });
    let first!: Promise<void>;
    act(() => {
      first = result.current.selectOption("DL333");
    });
    await act(async () => {
      await result.current.selectOption("JFK-HILTON");
    });
    expect(stream).toHaveBeenCalledTimes(1);
    expect((held.body as { content: string }).content).toBe(
      "Select Delta Air Lines flight",
    );

    const invalidate = vi.spyOn(qc, "invalidateQueries");
    await act(async () => {
      emitResult(held.cbs!, {
        clarify: false,
        shortlist: SHORTLIST,
        trip_id: "t1",
        trip_change: {
          action: "created",
          segment_types_added: ["flight"],
          segment_types_replaced: [],
          previous_trip: null,
          estimated_cost: "147.00",
          segment_count: 1,
        },
        trip_message: "Saved the Delta Air Lines flight to a new draft trip.",
        trip_notice: null,
      });
      await first;
    });
    expect(usePlannerStore.getState().selectedTripId).toBe("t1");
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Trip saved as a draft" }),
    );
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["trips"] });
    const turns = usePlannerStore.getState().turns.map((t) => t.content);
    expect(turns).toEqual([
      "Select Delta Air Lines flight",
      "Saved the Delta Air Lines flight to a new draft trip.",
    ]);
    expect(result.current.selecting).toBe(false);
  });

  it("a save failure is a toast only and the card is not left Selected", async () => {
    seedStore();
    const held = heldStream();
    const { result } = renderHook(() => useTripSelection(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client()}>{children}</QueryClientProvider>
      ),
    });
    let p!: Promise<void>;
    act(() => {
      p = result.current.selectOption("JFK-HILTON");
    });
    expect(usePlannerStore.getState().selectedOptionId).toBe("JFK-HILTON");
    await act(async () => {
      emitResult(held.cbs!, {
        clarify: false,
        shortlist: SHORTLIST,
        trip_id: null,
        trip_persist_error:
          "Action 'trip.update' requires permission 'trip.update.own'; principal does not have it.",
      });
      await p;
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: "error",
        title: "Could not save trip",
      }),
    );
    expect(result.current.selectError).toBeNull();
    expect(usePlannerStore.getState().selectedOptionId).toBeNull();
  });

  it("a selection turn that comes back without a trip clears the pending card", async () => {
    seedStore();
    const held = heldStream();
    const { result } = renderHook(() => useTripSelection(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client()}>{children}</QueryClientProvider>
      ),
    });
    let p!: Promise<void>;
    act(() => {
      p = result.current.selectOption("DL333");
    });
    await act(async () => {
      emitResult(held.cbs!, {
        clarify: true,
        question: "What is your departure date?",
      });
      await p;
    });
    expect(usePlannerStore.getState().selectedOptionId).toBeNull();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("a new draft started instead shows the notice", async () => {
    seedStore();
    const held = heldStream();
    const { result } = renderHook(() => useTripSelection(), {
      wrapper: ({ children }) => (
        <QueryClientProvider client={client()}>{children}</QueryClientProvider>
      ),
    });
    let p!: Promise<void>;
    act(() => {
      p = result.current.selectOption("JFK-HILTON");
    });
    await act(async () => {
      emitResult(held.cbs!, {
        clarify: false,
        shortlist: SHORTLIST,
        trip_id: "t2",
        trip_change: {
          action: "created",
          segment_types_added: ["hotel"],
          segment_types_replaced: [],
          previous_trip: {
            id: "t1",
            status: "pending_approval",
            label: "ORD → JFK",
            reason: "not_draft",
          },
          estimated_cost: "654.00",
          segment_count: 1,
        },
        trip_message:
          "Started a new draft trip … Saved the Hilton hotel to it.",
        trip_notice:
          "Started a new draft trip ORD → JFK — your earlier trip ORD → JFK is awaiting approval, so it can't be changed. It is still in Trips.",
      });
      await p;
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.objectContaining({
        variant: "info",
        title: "Trip saved — started a new draft trip",
      }),
    );
    expect(usePlannerStore.getState().selectedTripId).toBe("t2");
  });
});

// ---------------------------------------------------------------------------
// Components
// ---------------------------------------------------------------------------

describe("RecommendationPanel", () => {
  it("marks the flight AND the hotel on the trip as Selected", async () => {
    usePlannerStore.setState({
      intent: INTENT,
      shortlist: SHORTLIST,
      selectedTripId: "t1",
    });
    routeGet({
      "/trips/t1": trip(),
      "/trips/t1/segments": [FLIGHT_SEG, HOTEL_SEG],
    });
    wrap(<RecommendationPanel onSelectOption={vi.fn()} />);
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: /^Selected$/ }),
      ).toHaveLength(2),
    );
    // The other flight can still be picked (it would replace the saved one).
    expect(
      (
        screen.getByRole("button", {
          name: /Select United Airlines/,
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
  });
});

describe("PlannerTripStatus", () => {
  it("lists every saved leg and the estimated total", async () => {
    usePlannerStore.setState({ selectedTripId: "t1" });
    routeGet({
      "/trips/t1": trip(),
      "/trips/t1/segments": [FLIGHT_SEG, HOTEL_SEG],
    });
    wrap(<PlannerTripStatus />);
    expect(
      await screen.findByText("Flight · Delta Air Lines · $147.00"),
    ).toBeTruthy();
    expect(
      screen.getByText("Hotel · Hilton · 3 nights · $654.00"),
    ).toBeTruthy();
    expect(screen.getByText("Estimated total $801.00")).toBeTruthy();
    expect(
      screen.getByText("ORD → JFK · 2026-11-17 – 2026-11-20 · Client meeting"),
    ).toBeTruthy();
  });

  it("a submitted trip says the next selection starts a new draft", async () => {
    usePlannerStore.setState({ selectedTripId: "t1" });
    routeGet({
      "/trips/t1": trip({ status: "pending_approval" }),
      "/trips/t1/segments": [FLIGHT_SEG],
    });
    wrap(<PlannerTripStatus />);
    expect(
      await screen.findByText(
        /Selecting another option starts a new draft trip/,
      ),
    ).toBeTruthy();
  });

  it("a trip returned for changes is not edited by the planner", async () => {
    usePlannerStore.setState({ selectedTripId: "t1" });
    routeGet({
      "/trips/t1": trip({ approval_status: "changes_requested" }),
      "/trips/t1/segments": [FLIGHT_SEG],
    });
    wrap(<PlannerTripStatus />);
    expect(
      await screen.findByText(/This trip was returned for changes/),
    ).toBeTruthy();
    expect(
      screen.getByText(/Selecting another option here starts a new draft trip/),
    ).toBeTruthy();
  });

  it("a never-submitted draft shows no 'cannot change' caption", async () => {
    usePlannerStore.setState({ selectedTripId: "t1" });
    routeGet({
      "/trips/t1": trip(),
      "/trips/t1/segments": [FLIGHT_SEG],
    });
    wrap(<PlannerTripStatus />);
    await screen.findByText("Flight · Delta Air Lines · $147.00");
    expect(screen.queryByText(/starts a new draft trip/)).toBeNull();
  });

  it("Submit waits while a selection is being saved", async () => {
    usePlannerStore.setState({ selectedTripId: "t1" });
    routeGet({
      "/trips/t1": trip(),
      "/trips/t1/segments": [FLIGHT_SEG],
    });
    wrap(<PlannerTripStatus selecting />);
    const submit = await screen.findByRole("button", {
      name: /Submit for approval/,
    });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("TripList", () => {
  it("titles cards by route, dates and purpose and opens to the saved legs", async () => {
    routeGet({
      "/trips": {
        items: [
          {
            id: "t1",
            organization_id: "org",
            traveler_id: "emp",
            origin: "ORD",
            primary_destination: "JFK",
            start_date: "2026-11-17",
            end_date: "2026-11-20",
            purpose: "Client meeting",
            estimated_cost: "801.00",
            currency: "USD",
            status: "draft",
            version: 2,
            created_at: "2026-11-11T00:00:00Z",
            updated_at: "2026-11-11T00:00:00Z",
          },
        ],
        total: 1,
        page: 1,
        size: 50,
      },
      "/trips/t1/segments": [FLIGHT_SEG, HOTEL_SEG],
    });
    wrap(<TripList />);
    expect(
      await screen.findByText(
        "ORD → JFK · 2026-11-17 – 2026-11-20 · Client meeting",
      ),
    ).toBeTruthy();
    expect(screen.queryByText(/Hotel · Hilton/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show trip details" }));
    expect(
      await screen.findByText("Hotel · Hilton · 3 nights · $654.00"),
    ).toBeTruthy();
    expect(screen.getByText("Estimated total $801.00")).toBeTruthy();
  });
});
