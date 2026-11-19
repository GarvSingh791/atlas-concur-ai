/**
 * Atlas reusable AI Assistant store — Zustand (P5-T10).
 *
 * Global, app-shell–mounted state for the slide-out copilot so the conversation
 * survives route changes (in-memory) and reloads (sessionStorage + GET rehydrate).
 *
 * The client renders the server's truth only — it holds no generated content.
 * Confidence/route are captured from the live `status` SSE frame (not exposed by
 * GET), so they are present for live turns and honestly omitted on rehydration.
 *
 * Persistence mirrors the planner-store convention:
 *   - per-conversation turns:  `atlas-assistant-{conversationId}`
 *   - active pointer (context): `atlas-assistant-active`
 */

import { create } from "zustand";
import type { Citation, ConversationContextType } from "@/lib/types";
import type { ToolStep } from "@/lib/assistant/tool-activity";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/**
 * - grounded: a fully cited answer.
 * - silent: abstention (policy does not cover the question).
 * - needs_review: a LOW-CONFIDENCE policy answer — drafted cited text (if any)
 *   rendered with a caution label (C7-08).
 * - denied: a role-bounded refusal (NFR-SEC6) — the action is not permitted.
 */
export type AssistantAnswerType =
  | "grounded"
  | "silent"
  | "needs_review"
  | "denied";

export interface AssistantAnswer {
  answerType: AssistantAnswerType;
  answer: string | null;
  asserted?: string;
  critique?: string | null;
  reason?: string;
}

export interface AssistantTurn {
  id: string; // local unique id
  role: "user" | "assistant";
  content: string;
  timestamp: string; // ISO
  isStreaming?: boolean;
  error?: string | null;
  /** Structured policy/guardrail answer (grounded / silent / needs_review / denied). */
  answer?: AssistantAnswer | null;
  citations?: Citation[];
  /** Per-tool grounding steps from the persisted trace. */
  toolActivity?: ToolStep[];
  /** 0–100 grounding confidence for the policy surface. For POLICY answers this
   *  is the CALIBRATED score (from the result frame's output.confidence), mapped
   *  through the tau*-normalized display transform; otherwise the routing
   *  confidence (status frame). Null when unavailable (rehydration). */
  confidence?: number | null;
  /** 0–100 routing confidence from the live `status` frame — kept separate from
   *  the calibrated grounding confidence above. Null when unavailable. */
  routingConfidence?: number | null;
  /** Raw calibrated answer confidence (float 0..1) from result.output.confidence.
   *  Surfaced verbatim in the grounding-confidence tooltip. */
  calibratedConfidence?: number | null;
  /** "calibrated" | "cosine" | null — from result.output.confidence_source. */
  confidenceSource?: string | null;
  /** Calibrated relevance threshold tau* (float 0..1) from result.output.threshold. */
  threshold?: number | null;
  /** "deterministic" | "llm" — from the live `status` frame. */
  route?: string | null;
  needsReview?: boolean;
  /** Silent-state escalation CTA from result.output.escalation. */
  escalation?: { message: string; secondary?: string } | null;
  /** Silent-state near-topic hint from result.output.near_topic. */
  nearTopic?: string | null;
}

/** What triggered the conversation — drives the context_type sent to the API. */
export interface AssistantContext {
  contextType: ConversationContextType;
  contextRefId: string | null;
  /** Human label for the context chip, e.g. "NYC Conference". */
  contextLabel: string | null;
}

export interface AssistantState extends AssistantContext {
  conversationId: string | null;
  turns: AssistantTurn[];
  streaming: boolean;
  error: string | null;
  /** Drawer open state — global so any screen can open the assistant. */
  isOpen: boolean;
}

export interface AssistantActions {
  setConversationId: (id: string | null) => void;
  open: () => void;
  close: () => void;
  toggle: () => void;
  /** Scope to a subject AND open the drawer (imperative entry from a screen). */
  openScoped: (ctx: Partial<AssistantContext>) => void;
  /**
   * Scope (or re-scope) the assistant. If the context changed, the active
   * conversation is reset so the new session is about the new subject.
   */
  openWithContext: (ctx: Partial<AssistantContext>) => void;
  addTurn: (turn: AssistantTurn) => void;
  updateTurn: (id: string, patch: Partial<AssistantTurn>) => void;
  setTurns: (turns: AssistantTurn[]) => void;
  setStreaming: (streaming: boolean) => void;
  setError: (error: string | null) => void;
  reset: () => void;
  // sessionStorage persistence
  persistToSession: () => void;
  /** Restore the active pointer + that conversation's turns. Returns the id. */
  hydrateFromSession: () => string | null;
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const INITIAL_STATE: AssistantState = {
  conversationId: null,
  contextType: "general",
  contextRefId: null,
  contextLabel: null,
  turns: [],
  streaming: false,
  error: null,
  isOpen: false,
};

const ACTIVE_KEY = "atlas-assistant-active";

function convKey(conversationId: string): string {
  return `atlas-assistant-${conversationId}`;
}

interface PersistedConversation {
  conversationId: string | null;
  contextType: ConversationContextType;
  contextRefId: string | null;
  contextLabel: string | null;
  turns: AssistantTurn[];
}

function sameContext(
  a: AssistantContext,
  b: Partial<AssistantContext>,
): boolean {
  return (
    (b.contextType === undefined || a.contextType === b.contextType) &&
    (b.contextRefId === undefined || a.contextRefId === b.contextRefId)
  );
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

export const useAssistantStore = create<AssistantState & AssistantActions>(
  (set, get) => ({
    ...INITIAL_STATE,

    setConversationId: (id) => set({ conversationId: id }),

    open: () => set({ isOpen: true }),

    close: () => set({ isOpen: false }),

    toggle: () => set((s) => ({ isOpen: !s.isOpen })),

    openScoped: (ctx) => {
      get().openWithContext(ctx);
      set({ isOpen: true });
    },

    openWithContext: (ctx) => {
      const current = get();
      const next: AssistantContext = {
        contextType: ctx.contextType ?? current.contextType,
        contextRefId: ctx.contextRefId ?? null,
        contextLabel: ctx.contextLabel ?? null,
      };
      // Re-scoping to a different subject starts a fresh conversation.
      if (!sameContext(current, next)) {
        set({
          ...next,
          conversationId: null,
          turns: [],
          error: null,
          streaming: false,
        });
      } else {
        set(next);
      }
    },

    addTurn: (turn) => set((s) => ({ turns: [...s.turns, turn] })),

    updateTurn: (id, patch) =>
      set((s) => ({
        turns: s.turns.map((t) => (t.id === id ? { ...t, ...patch } : t)),
      })),

    setTurns: (turns) => set({ turns }),

    setStreaming: (streaming) => set({ streaming }),

    setError: (error) => set({ error }),

    reset: () => set({ ...INITIAL_STATE }),

    persistToSession: () => {
      if (typeof window === "undefined") return;
      const s = get();
      if (!s.conversationId) return;
      const payload: PersistedConversation = {
        conversationId: s.conversationId,
        contextType: s.contextType,
        contextRefId: s.contextRefId,
        contextLabel: s.contextLabel,
        // Never persist a half-streamed turn as still streaming.
        turns: s.turns.map((t) => ({ ...t, isStreaming: false })),
      };
      try {
        sessionStorage.setItem(
          convKey(s.conversationId),
          JSON.stringify(payload),
        );
        sessionStorage.setItem(
          ACTIVE_KEY,
          JSON.stringify({
            conversationId: s.conversationId,
            contextType: s.contextType,
            contextRefId: s.contextRefId,
            contextLabel: s.contextLabel,
          }),
        );
      } catch {
        // sessionStorage unavailable (private mode / quota) — non-fatal.
      }
    },

    hydrateFromSession: () => {
      if (typeof window === "undefined") return null;
      try {
        const activeRaw = sessionStorage.getItem(ACTIVE_KEY);
        if (!activeRaw) return null;
        const active = JSON.parse(activeRaw) as {
          conversationId: string | null;
          contextType?: ConversationContextType;
          contextRefId?: string | null;
          contextLabel?: string | null;
        };
        if (!active.conversationId) return null;

        const convRaw = sessionStorage.getItem(convKey(active.conversationId));
        const conv = convRaw
          ? (JSON.parse(convRaw) as PersistedConversation)
          : null;

        set({
          conversationId: active.conversationId,
          contextType: conv?.contextType ?? active.contextType ?? "general",
          contextRefId: conv?.contextRefId ?? active.contextRefId ?? null,
          contextLabel: conv?.contextLabel ?? active.contextLabel ?? null,
          turns: conv?.turns ?? [],
          streaming: false,
          error: null,
        });
        return active.conversationId;
      } catch {
        return null;
      }
    },
  }),
);

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

export const selectTurns = (s: AssistantState) => s.turns;
export const selectStreaming = (s: AssistantState) => s.streaming;
export const selectContextLabel = (s: AssistantState) => s.contextLabel;
