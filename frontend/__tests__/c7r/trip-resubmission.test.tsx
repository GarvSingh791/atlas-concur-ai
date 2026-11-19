/**
 * C7R review — a trip returned for changes goes back to `draft`; resubmitting
 * it (PATCH /trips/{id} status=pending_approval) re-queues its approval. The
 * frontend had NO way to submit or resubmit a trip, did not say a trip was
 * returned (or why), put a never-submitted draft on the "Approval" step, and
 * toasted "pending approval" for a trip the agent saves as a draft.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import * as React from "react";
import {
  render,
  screen,
  fireEvent,
  cleanup,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { showToast, patch, get, session } = vi.hoisted(() => ({
  showToast: vi.fn(),
  patch: vi.fn(),
  get: vi.fn(),
  // UAT2: only the trip's traveler ("emp" in the fixture) may (re)submit it.
  session: { user: { id: "emp" } as { id: string } | null },
}));
vi.mock("@/lib/auth/session-provider", () => ({
  useSession: () => session,
}));
vi.mock("@/components/atlas/atlas-toast", () => ({
  useAtlasToast: () => ({ showToast }),
}));
vi.mock("@/lib/api/client", () => ({
  api: { get, post: vi.fn(), patch },
}));

import { TripApprovalPanel } from "@/components/trips/trip-approval-panel";
import { PlannerStepper } from "@/components/planner/planner-stepper";
import { PlannerTripStatus } from "@/components/planner/planner-trip-status";
import { tripApprovalView } from "@/lib/trips/approval";
import { usePlannerStore } from "@/stores/planner-store";
import type { Trip } from "@/lib/types";

function trip(over: Partial<Trip> = {}): Trip {
  return {
    id: "t1",
    organization_id: "org",
    traveler_id: "emp",
    origin: "SFO",
    primary_destination: "JFK",
    start_date: "2026-06-10",
    end_date: "2026-06-12",
    purpose: null,
    estimated_cost: null,
    budget_amount: null,
    currency: "USD",
    status: "draft",
    recommendation_context: null,
    created_by: null,
    updated_by: null,
    created_at: "2026-06-01T00:00:00Z",
    updated_at: "2026-06-01T00:00:00Z",
    version: 2,
    approval_status: null,
    approval_reason: null,
    ...over,
  };
}

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  showToast.mockReset();
  patch.mockReset();
  get.mockReset();
  session.user = { id: "emp" };
});
afterEach(() => cleanup());

describe("tripApprovalView", () => {
  it("a returned draft is 'Changes requested' with a resubmit action", () => {
    const v = tripApprovalView({
      status: "draft",
      approval_status: "changes_requested",
    });
    expect(v.stage).toBe("returned");
    expect(v.submitLabel).toBe("Resubmit for approval");
  });
  it("a never-submitted draft can be submitted; a pending trip cannot", () => {
    expect(tripApprovalView({ status: "draft" }).submitLabel).toBe(
      "Submit for approval",
    );
    expect(
      tripApprovalView({ status: "pending_approval" }).submitLabel,
    ).toBeNull();
    expect(tripApprovalView({ status: "approved" }).submitLabel).toBeNull();
  });
});

describe("TripApprovalPanel", () => {
  it("shows the approver's comment and resubmits through PATCH /trips/{id}", async () => {
    patch.mockResolvedValue(
      trip({ status: "pending_approval", approval_status: "pending" }),
    );
    wrap(
      <TripApprovalPanel
        trip={trip({
          approval_status: "changes_requested",
          approval_reason: "Pick a cheaper hotel",
        })}
      />,
    );
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Pick a cheaper hotel")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: /Resubmit for approval/ }),
    );
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/trips/t1", {
        body: { status: "pending_approval" },
      }),
    );
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "success",
          title: "Trip resubmitted",
        }),
      ),
    );
  });

  it("a pending trip has no submit action and shows no stale reason", () => {
    wrap(
      <TripApprovalPanel
        trip={trip({
          status: "pending_approval",
          approval_status: "pending",
          approval_reason: "old comment",
        })}
      />,
    );
    expect(screen.getByText("Awaiting approval")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText("old comment")).toBeNull();
  });
});

// UAT2 defect E follow-up — the owner can discard a never-submitted draft.
describe("discard a draft trip", () => {
  it("the owner discards a never-submitted draft (PATCH status=cancelled) after confirming", async () => {
    patch.mockResolvedValue(trip({ status: "cancelled" }));
    wrap(<TripApprovalPanel trip={trip()} allowDiscard />);
    fireEvent.click(screen.getByRole("button", { name: /Discard draft/ }));
    expect(patch).not.toHaveBeenCalled(); // confirm first
    fireEvent.click(screen.getByRole("button", { name: "Discard trip" }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/trips/t1", {
        body: { status: "cancelled" },
      }),
    );
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        expect.objectContaining({
          variant: "success",
          title: "Draft trip discarded",
        }),
      ),
    );
  });

  it("never offered for a returned draft, a submitted trip, a non-owner, or without allowDiscard", () => {
    const cases: Array<[Partial<Trip>, boolean, { id: string }]> = [
      [{ approval_status: "changes_requested" }, true, { id: "emp" }],
      [
        { status: "pending_approval", approval_status: "pending" },
        true,
        { id: "emp" },
      ],
      [
        { status: "approved", approval_status: "approved" },
        true,
        { id: "emp" },
      ],
      [{}, true, { id: "mgr" }],
      [{}, false, { id: "emp" }],
    ];
    for (const [over, allowDiscard, user] of cases) {
      session.user = user;
      wrap(<TripApprovalPanel trip={trip(over)} allowDiscard={allowDiscard} />);
      expect(screen.queryByRole("button", { name: /Discard/ })).toBeNull();
      cleanup();
    }
  });
});

// UAT2 Issue 7 — a reader who is not the traveler never gets the action.
describe("non-owner trip view", () => {
  it("tripApprovalView with viewerIsOwner=false never offers a submit label", () => {
    const statuses = [
      { status: "draft" as const },
      { status: "draft" as const, approval_status: "changes_requested" },
      { status: "pending_approval" as const },
      { status: "approved" as const },
      { status: "rejected" as const },
      { status: "cancelled" as const },
    ];
    for (const t of statuses) {
      const v = tripApprovalView(t, { viewerIsOwner: false });
      expect(v.submitLabel).toBeNull();
      expect(v.description).not.toMatch(/Your approver|resubmit|Submit it/i);
    }
    // Stage + label are unchanged — only the action and voice differ.
    expect(
      tripApprovalView(
        { status: "draft", approval_status: "changes_requested" },
        { viewerIsOwner: false },
      ),
    ).toMatchObject({ stage: "returned", label: "Changes requested" });
  });

  it("TripApprovalPanel: a manager viewing a returned trip gets no resubmit and neutral copy", () => {
    session.user = { id: "mgr" };
    wrap(
      <TripApprovalPanel
        trip={trip({
          approval_status: "changes_requested",
          approval_reason: "Pick a cheaper hotel",
          traveler_name: "Erin Employee",
        })}
      />,
    );
    expect(screen.getByText("Changes requested")).toBeTruthy();
    expect(screen.getByText("Pick a cheaper hotel")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByText(/Your approver/)).toBeNull();
    expect(
      screen.getByText(
        "The approver sent this trip back to its traveler for changes.",
      ),
    ).toBeTruthy();
    expect(screen.getByRole("note").textContent).toMatch(
      /Viewing Erin Employee’s trip\s*—\s*read-only/,
    );
  });

  it("TripApprovalPanel: no action and no banner while the session hydrates", () => {
    session.user = null;
    wrap(<TripApprovalPanel trip={trip()} />);
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("note")).toBeNull();
  });

  it("PlannerTripStatus with a teammate's trip (persisted selection) renders no submit", async () => {
    session.user = { id: "mgr" };
    get.mockResolvedValue(trip());
    usePlannerStore.setState({ selectedTripId: "t1" });
    wrap(<PlannerTripStatus />);
    await waitFor(() => expect(screen.getByText("Draft")).toBeTruthy());
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByRole("note").textContent).toContain(
      "Viewing another traveler’s trip",
    );
  });

  it("the traveler still sees Submit for approval", () => {
    wrap(<TripApprovalPanel trip={trip()} />);
    expect(
      screen.getByRole("button", { name: /Submit for approval/ }),
    ).toBeTruthy();
    expect(screen.queryByRole("note")).toBeNull();
  });
});

describe("PlannerStepper", () => {
  function activeStep(): string | null {
    const el = document.querySelector('[aria-current="step"]');
    return el?.textContent ?? null;
  }

  it("a saved but unsubmitted draft is on Selection, not Approval", async () => {
    get.mockResolvedValue(trip());
    usePlannerStore.setState({ selectedTripId: "t1" });
    wrap(<PlannerStepper />);
    await waitFor(() => expect(get).toHaveBeenCalledWith("/trips/t1"));
    await waitFor(() => expect(activeStep()).toContain("Selection"));
  });

  it("a returned trip and a pending trip are on Approval", async () => {
    get.mockResolvedValue(trip({ approval_status: "changes_requested" }));
    usePlannerStore.setState({ selectedTripId: "t1" });
    wrap(<PlannerStepper />);
    await waitFor(() => expect(activeStep()).toContain("Approval"));
  });
});
