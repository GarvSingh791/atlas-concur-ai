/**
 * Atlas planner store — Zustand.
 *
 * Manages structured state for the Travel Planner UI across two conversation
 * modes: 'plan' (travel_planning) and 'policy' (policy_qa).
 *
 * Persistence: keyed by conversation ID in sessionStorage so the screen
 * survives a browser reload even though structured state (chips, shortlist,
 * clarify questions) is not persisted by the backend.
 */

import type { PlannerPolicyAnswer } from "@/lib/planner/use-planner-conversation";
import { create } from "zustand";
import type { PlannerIntent, ShortlistOption, Citation } from "@/lib/types";
import { EMPTY_INTENT } from "@/lib/planner/intent";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type PlannerMode = "plan" | "policy";

export interface PlannerTurn {
  id: string; // local unique ID
  role: "user" | "assistant";
  content: string;
  timestamp: string; // ISO string
  isStreaming?: boolean;
  /** C7-07: structured policy answer (Ask Policy mode) rendered with its
   *  citations and trust label instead of a plain bubble. */
  policyAnswer?: PlannerPolicyAnswer;
}

export interface PlannerState {
  // Conversation IDs
  conversationId: string | null;
  policyConversationId: string | null;

  // Current intent (client-held structured slots)
  intent: PlannerIntent;

  // Conversation history (text turns for display)
  turns: PlannerTurn[];

  // Current shortlist from the agent
  shortlist: ShortlistOption[];
  citations: Citation[];

  // Selection state
  selectedOptionId: string | null;
  selectedTripId: string | null;

  // Mode toggle
  mode: PlannerMode;

  // Stream state
  streaming: boolean;
  clarifyQuestion: string | null;
  // Server explanation when a plan returned no options (shown in the panel)
  noOptionsMessage: string | null;
  streamingText: string; // accumulated narrative
}

export interface PlannerActions {
  setConversationId: (id: string) => void;
  setPolicyConversationId: (id: string) => void;
  setIntent: (intent: Partial<PlannerIntent>) => void;
  addTurn: (turn: PlannerTurn) => void;
  setShortlist: (shortlist: ShortlistOption[], citations: Citation[]) => void;
  setClarifyQuestion: (q: string | null) => void;
  setNoOptionsMessage: (message: string | null) => void;
  setSelectedOption: (optionId: string | null) => void;
  setSelectedTripId: (tripId: string | null) => void;
  setMode: (mode: PlannerMode) => void;
  setStreaming: (streaming: boolean) => void;
  setStreamingText: (text: string) => void;
  appendStreamingText: (chunk: string) => void;
  reset: () => void;
  // sessionStorage persistence
  persistToSession: (conversationId: string) => void;
  hydrateFromSession: (conversationId: string) => void;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const INITIAL_STATE: PlannerState = {
  conversationId: null,
  policyConversationId: null,
  intent: EMPTY_INTENT,
  turns: [],
  shortlist: [],
  citations: [],
  selectedOptionId: null,
  selectedTripId: null,
  mode: "plan",
  streaming: false,
  clarifyQuestion: null,
  noOptionsMessage: null,
  streamingText: "",
};

// ---------------------------------------------------------------------------
// Session persistence helpers
// ---------------------------------------------------------------------------

type PersistedPlannerState = Pick<
  PlannerState,
  | "conversationId"
  | "policyConversationId"
  | "intent"
  | "turns"
  | "shortlist"
  | "citations"
  | "noOptionsMessage"
  | "selectedOptionId"
  | "selectedTripId"
  | "mode"
>;

function sessionKey(conversationId: string): string {
  return `atlas-planner-${conversationId}`;
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const usePlannerStore = create<PlannerState & PlannerActions>(
  (set, get) => ({
    ...INITIAL_STATE,

    // --- Conversation IDs ---------------------------------------------------

    setConversationId: (id) => set({ conversationId: id }),

    setPolicyConversationId: (id) => set({ policyConversationId: id }),

    // --- Intent -------------------------------------------------------------

    setIntent: (partial) =>
      set((s) => ({ intent: { ...s.intent, ...partial } })),

    // --- Turns --------------------------------------------------------------

    addTurn: (turn) => set((s) => ({ turns: [...s.turns, turn] })),

    // --- Shortlist ----------------------------------------------------------

    setShortlist: (shortlist, citations) => set({ shortlist, citations }),

    // --- Clarify ------------------------------------------------------------

    setClarifyQuestion: (q) => set({ clarifyQuestion: q }),

    setNoOptionsMessage: (message) => set({ noOptionsMessage: message }),

    // --- Selection ----------------------------------------------------------

    setSelectedOption: (optionId) => set({ selectedOptionId: optionId }),

    setSelectedTripId: (tripId) => set({ selectedTripId: tripId }),

    // --- Mode ---------------------------------------------------------------

    setMode: (mode) => set({ mode }),

    // --- Streaming ----------------------------------------------------------

    setStreaming: (streaming) => set({ streaming }),

    setStreamingText: (text) => set({ streamingText: text }),

    appendStreamingText: (chunk) =>
      set((s) => ({
        streamingText: s.streamingText ? s.streamingText + " " + chunk : chunk,
      })),

    // --- Reset --------------------------------------------------------------

    reset: () => set(INITIAL_STATE),

    // --- Session persistence ------------------------------------------------

    persistToSession: (conversationId) => {
      if (typeof window === "undefined") return;
      const state = get();
      const persisted: PersistedPlannerState = {
        conversationId: state.conversationId,
        policyConversationId: state.policyConversationId,
        intent: state.intent,
        turns: state.turns,
        shortlist: state.shortlist,
        citations: state.citations,
        noOptionsMessage: state.noOptionsMessage,
        selectedOptionId: state.selectedOptionId,
        selectedTripId: state.selectedTripId,
        mode: state.mode,
      };
      try {
        sessionStorage.setItem(
          sessionKey(conversationId),
          JSON.stringify(persisted),
        );
      } catch {
        // sessionStorage may be unavailable (private mode, quota exceeded)
      }
    },

    hydrateFromSession: (conversationId) => {
      if (typeof window === "undefined") return;
      try {
        const raw = sessionStorage.getItem(sessionKey(conversationId));
        if (!raw) return;
        const persisted = JSON.parse(raw) as PersistedPlannerState;
        set({
          conversationId: persisted.conversationId,
          policyConversationId: persisted.policyConversationId,
          intent: persisted.intent ?? EMPTY_INTENT,
          turns: persisted.turns ?? [],
          shortlist: persisted.shortlist ?? [],
          citations: persisted.citations ?? [],
          noOptionsMessage: persisted.noOptionsMessage ?? null,
          selectedOptionId: persisted.selectedOptionId,
          selectedTripId: persisted.selectedTripId,
          mode: persisted.mode ?? "plan",
        });
      } catch {
        // Corrupt data — ignore and leave store at current state
      }
    },
  }),
);

// ---------------------------------------------------------------------------
// Selectors (prefer these over raw state access in components)
// ---------------------------------------------------------------------------

export const selectIntent = (s: PlannerState) => s.intent;
export const selectShortlist = (s: PlannerState) => s.shortlist;
export const selectSelectedTripId = (s: PlannerState) => s.selectedTripId;
export const selectMode = (s: PlannerState) => s.mode;
export const selectStreaming = (s: PlannerState) => s.streaming;
