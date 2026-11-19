"use client";

import * as React from "react";
import { streamMessages } from "@/lib/api/stream";
import { parseSSEFrame, discriminateOutput } from "./sse-events";
import { toMessagePayload } from "./intent";
import { savedOptionIds } from "./selected-options";
import { usePlannerStore } from "@/stores/planner-store";
import { useAtlasToast } from "@/components/atlas/atlas-toast";
import { useQueryClient } from "@tanstack/react-query";
import {
  fetchTrip,
  fetchTripSegments,
  tripQueryKeys,
  useTrip,
  useTripSegments,
} from "@/lib/query/trip-hooks";
import type { AtlasToastOptions } from "@/components/atlas/atlas-toast";
import type { ShortlistOption, TripChange } from "@/lib/types";
import { formatUsd } from "@/lib/trips/summary";

export interface UseTripSelectionResult {
  selectOption: (optionId: string) => Promise<void>;
  selecting: boolean;
  selectError: string | null;
}

let _selectionTurn = 0;
function turnId(): string {
  return `sel-${++_selectionTurn}-${Date.now()}`;
}

/** "Select Delta Air Lines flight" — so the conversation history says what was picked. */
export function selectionContent(option: ShortlistOption | undefined): string {
  if (!option) return "Plan my trip";
  return `Select ${option.supplier} ${option.segment_type}`;
}

/**
 * The toast for a selection's outcome (UAT-2 Issue 4: one planning conversation
 * -> one draft trip). Every saved outcome keeps the "Trip saved" title prefix.
 * Re-picking the option already on the trip shows nothing.
 */
export function selectionToast(
  change: TripChange,
  notice: string | null | undefined,
): AtlasToastOptions | null {
  if (notice) {
    return {
      variant: "info",
      title: "Trip saved — started a new draft trip",
      description: notice,
    };
  }
  const cost = formatUsd(change.estimated_cost);
  const costLine = cost ? `Estimated cost ${cost}.` : undefined;
  if (change.action === "created") {
    return {
      variant: "success",
      title: "Trip saved as a draft",
      description: "Submit it for approval when you're ready.",
    };
  }
  if (change.action === "updated" && change.end_date_change) {
    // UAT2 defect E: only the return date changed -> the same draft, in place.
    const lines = [`Return date moved to ${change.end_date_change.to}.`];
    if (change.stale_hotel) {
      lines.push(
        "The hotel still covers the old dates — select a hotel to cover the new stay.",
      );
    } else if (costLine) {
      lines.push(costLine);
    }
    return {
      variant: change.stale_hotel ? "info" : "success",
      title: "Trip saved — return date updated",
      description: lines.join(" "),
    };
  }
  if (change.action === "updated") {
    const added = change.segment_types_added[0];
    const replaced = change.segment_types_replaced[0];
    const title = added
      ? `Trip saved — ${added} added`
      : replaced
        ? `Trip saved — ${replaced} replaced`
        : "Trip saved";
    return { variant: "success", title, description: costLine };
  }
  return null;
}

/**
 * Option ids shown as "Selected": the legs saved on the conversation's trip
 * (when it still matches the request), plus the option being saved right now.
 */
export function useSelectedOptionIds(pendingId: string | null): Set<string> {
  const selectedTripId = usePlannerStore((s) => s.selectedTripId);
  const intent = usePlannerStore((s) => s.intent);
  const { data: trip } = useTrip(selectedTripId);
  const { data: segments } = useTripSegments(selectedTripId);
  return React.useMemo(() => {
    const ids = savedOptionIds(trip, segments, intent);
    if (pendingId) ids.add(pendingId);
    return ids;
  }, [trip, segments, intent, pendingId]);
}

export function useTripSelection(): UseTripSelectionResult {
  const [selecting, setSelecting] = React.useState(false);
  const [selectError, setSelectError] = React.useState<string | null>(null);
  const { showToast } = useAtlasToast();
  const qc = useQueryClient();

  const intent = usePlannerStore((s) => s.intent);
  const conversationId = usePlannerStore((s) => s.conversationId);
  const setSelectedOption = usePlannerStore((s) => s.setSelectedOption);
  const setSelectedTripId = usePlannerStore((s) => s.setSelectedTripId);
  const addTurn = usePlannerStore((s) => s.addTurn);
  const persistToSession = usePlannerStore((s) => s.persistToSession);

  // Re-entrancy guard: a second click while a selection is being saved is
  // ignored (never cancels the first — its result must not be lost).
  const inFlightRef = React.useRef(false);

  const selectOption = React.useCallback(
    async (optionId: string) => {
      if (!conversationId) {
        setSelectError(
          "No active conversation. Please describe your trip first.",
        );
        return;
      }
      if (inFlightRef.current) return;
      inFlightRef.current = true;

      setSelecting(true);
      setSelectError(null);
      setSelectedOption(optionId);

      const option = usePlannerStore
        .getState()
        .shortlist.find((o) => o.option_id === optionId);
      const content = selectionContent(option);
      addTurn({
        id: turnId(),
        role: "user",
        content,
        timestamp: new Date().toISOString(),
      });

      const payload = toMessagePayload(intent, {
        content,
        selectedOptionIds: [optionId],
        conversationId,
      });

      // Cleared unless this selection was saved: a card must never claim
      // "Selected" for an option that is not on the trip.
      let saved = false;
      // The saved trip's queries, refreshed BEFORE the pending card is released
      // so it goes straight from "saving" to "Selected" (no flicker).
      let refreshed: Promise<unknown> = Promise.resolve();

      return new Promise<void>((resolve) => {
        let finished = false;
        const finish = () => {
          if (finished) return;
          finished = true;
          void refreshed.finally(() => {
            if (!saved) setSelectedOption(null);
            inFlightRef.current = false;
            setSelecting(false);
            resolve();
          });
        };

        streamMessages(
          conversationId,
          payload as unknown as Record<string, unknown>,
          {
            onMessage: () => {},
            onEvent: (event, data) => {
              const frame = parseSSEFrame(event, data);
              if (!frame) return;

              if (frame.event === "result") {
                const output = discriminateOutput(frame.data.output);
                if (!output || !("clarify" in output) || output.clarify) {
                  return;
                }
                const reply = output.trip_message ?? output.trip_persist_error;
                if (reply) {
                  addTurn({
                    id: turnId(),
                    role: "assistant",
                    content: reply,
                    timestamp: new Date().toISOString(),
                  });
                }
                if (output.trip_persist_error) {
                  // A toast only: the shortlist stays usable (selectError would
                  // replace it with a full-panel error).
                  showToast({
                    variant: "error",
                    title: "Could not save trip",
                    description: output.trip_persist_error,
                  });
                }
                if (output.trip_id) {
                  saved = true;
                  const tripId = output.trip_id;
                  setSelectedTripId(tripId);
                  // Every list / detail / segments query of trips is stale now;
                  // the saved trip's own detail + legs are fetched right away.
                  refreshed = Promise.allSettled([
                    qc.invalidateQueries({ queryKey: ["trips"] }),
                    qc.fetchQuery({
                      queryKey: tripQueryKeys.trip(tripId),
                      queryFn: () => fetchTrip(tripId),
                    }),
                    qc.fetchQuery({
                      queryKey: tripQueryKeys.tripSegments(tripId),
                      queryFn: () => fetchTripSegments(tripId),
                    }),
                  ]);
                  if (output.trip_change) {
                    const toast = selectionToast(
                      output.trip_change,
                      output.trip_notice,
                    );
                    if (toast) showToast(toast);
                  } else {
                    showToast({
                      variant: "success",
                      title: "Trip saved as a draft",
                      description: "Submit it for approval when you're ready.",
                    });
                  }
                }
                const activeConvId = usePlannerStore.getState().conversationId;
                if (activeConvId) persistToSession(activeConvId);
              }

              if (frame.event === "done") finish();
            },
            onError: (err) => {
              setSelectError(err.message);
              showToast({
                variant: "error",
                title: "Selection failed",
                description: err.message,
              });
              finish();
            },
            onComplete: () => finish(),
          },
        );
      });
    },
    [
      conversationId,
      intent,
      setSelectedOption,
      setSelectedTripId,
      addTurn,
      persistToSession,
      showToast,
      qc,
    ],
  );

  return { selectOption, selecting, selectError };
}
