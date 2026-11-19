"use client";

import * as React from "react";
import { streamMessages } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import type {
  Conversation,
  MessageCreate,
  PlannerIntentSlots,
  ShortlistOption,
  Citation,
} from "@/lib/types";
import { parseSSEFrame, discriminateOutput } from "./sse-events";

/** A policy answer carried by a result frame (C7-07). */
export interface PlannerPolicyAnswer {
  answerType: "grounded" | "silent" | "needs_review";
  /** Displayable text (grounded answer or needs_review draft); null otherwise. */
  answer: string | null;
  /** Abstention reason / rationale (silent). */
  reason?: string;
  citations: Citation[];
  escalation?: { message: string; secondary?: string } | null;
  nearTopic?: string | null;
}

/** What sendMessage resolves with: the final state of THAT stream. */
export interface PlannerSendResult extends PlannerConversationState {
  /** True when the stream was cancelled / superseded before it settled. */
  cancelled: boolean;
}

export interface PlannerConversationState {
  conversationId: string | null;
  streaming: boolean;
  streamingText: string; // accumulated narrative chunks
  shortlist: ShortlistOption[];
  citations: Citation[];
  clarifyQuestion: string | null;
  /** True when THIS stream's result carried a shortlist (possibly empty). */
  shortlistReturned: boolean;
  /** Server explanation for an empty shortlist ("No options found ..."). */
  message: string | null;
  /** The intent the agent resolved this turn (prior + chips + message text). */
  intent: PlannerIntentSlots | null;
  tripId: string | null;
  /** C7-07: the policy answer from the last result frame (policy_qa mode). */
  policyAnswer: PlannerPolicyAnswer | null;
  /** Plain message body from a result frame with no structured answer. */
  resultMessage: string | null;
  done: boolean;
  error: string | null;
}

const INITIAL_STATE: PlannerConversationState = {
  conversationId: null,
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
  done: false,
  error: null,
};

function asText(v: unknown): string | null {
  return typeof v === "string" && v.trim() !== "" ? v : null;
}

/** Map a result frame's output onto a PlannerPolicyAnswer (or null). */
export function toPlannerPolicyAnswer(
  output: Record<string, unknown>,
  frameCitations: Citation[],
): PlannerPolicyAnswer | null {
  const disc = discriminateOutput(output);
  if (!disc || !("answer_type" in disc)) return null;
  if (disc.answer_type === "grounded") {
    return {
      answerType: "grounded",
      answer: disc.answer || null,
      citations: frameCitations,
    };
  }
  if (disc.answer_type === "silent") {
    const esc = output.escalation;
    let escalation: PlannerPolicyAnswer["escalation"] = null;
    if (esc && typeof esc === "object") {
      const e = esc as Record<string, unknown>;
      const message = asText(e.message);
      if (message) {
        escalation = { message, secondary: asText(e.secondary) ?? undefined };
      }
    }
    return {
      answerType: "silent",
      answer: null,
      reason: asText(output.rationale) ?? (disc.reason || undefined),
      citations: [],
      escalation,
      nearTopic: asText(output.near_topic),
    };
  }
  return {
    answerType: "needs_review",
    answer: disc.answer,
    citations: frameCitations.length > 0 ? frameCitations : disc.citations,
  };
}

export function usePlannerConversation(initialConversationId?: string | null) {
  const [state, setState] = React.useState<PlannerConversationState>({
    ...INITIAL_STATE,
    conversationId: initialConversationId ?? null,
  });
  const cancelRef = React.useRef<(() => void) | null>(null);

  // C7-07: a synchronously-updated mirror of the state so sendMessage can
  // resolve with the FINAL state of its stream. Callers used to read
  // `hook.state` after awaiting — a stale render closure that never saw the
  // answer.
  const stateRef = React.useRef<PlannerConversationState>(state);
  const update = React.useCallback(
    (fn: (s: PlannerConversationState) => PlannerConversationState) => {
      stateRef.current = fn(stateRef.current);
      const next = stateRef.current;
      setState(next);
    },
    [],
  );

  // The id can arrive after the first render (the ?c= URL id is hydrated into
  // the store in an effect on reload). Adopt it, so the next message continues
  // THAT conversation instead of silently creating a new one.
  React.useEffect(() => {
    if (
      initialConversationId &&
      initialConversationId !== stateRef.current.conversationId
    ) {
      update((s) => ({ ...s, conversationId: initialConversationId }));
    }
  }, [initialConversationId, update]);

  // Ensure a conversation exists (lazy create if needed)
  const ensureConversation = React.useCallback(
    async (contextType: "travel_planning" | "policy_qa"): Promise<string> => {
      const existing = stateRef.current.conversationId;
      if (existing) return existing;
      const conv = await api.post<Conversation>("/conversations", {
        body: { context_type: contextType },
      });
      update((s) => ({ ...s, conversationId: conv.id }));
      return conv.id;
    },
    [update],
  );

  const sendMessage = React.useCallback(
    async (
      payload: MessageCreate,
      contextType: "travel_planning" | "policy_qa" = "travel_planning",
    ): Promise<PlannerSendResult> => {
      // Cancel any in-flight stream
      if (cancelRef.current) {
        cancelRef.current();
        cancelRef.current = null;
      }

      update((s) => ({
        ...s,
        streaming: true,
        streamingText: "",
        clarifyQuestion: null,
        shortlistReturned: false,
        message: null,
        intent: null,
        policyAnswer: null,
        resultMessage: null,
        done: false,
        error: null,
      }));

      let convId: string;
      try {
        convId = await ensureConversation(contextType);
      } catch (err) {
        update((s) => ({
          ...s,
          streaming: false,
          error:
            err instanceof Error
              ? err.message
              : "Failed to create conversation",
        }));
        return { ...stateRef.current, cancelled: false };
      }

      // Resolve once the stream settles (complete, error or cancel) so the
      // caller can await the real outcome — the transport is fire-and-forget.
      let cancelled = false;
      let finalState: PlannerConversationState | null = null;
      let settle: () => void = () => {};
      const settled = new Promise<void>((resolve) => {
        settle = resolve;
      });

      const abort = streamMessages(
        convId,
        payload as unknown as Record<string, unknown>,
        {
          onMessage: () => {}, // legacy; we use onEvent instead
          onEvent: (event, data) => {
            const frame = parseSSEFrame(event, data);
            if (!frame) return;

            if (frame.event === "narrative") {
              update((s) => ({
                ...s,
                streamingText:
                  s.streamingText +
                  (s.streamingText ? " " : "") +
                  frame.data.text,
              }));
              return;
            }

            if (frame.event === "result") {
              const out = frame.data.output;
              const citations = frame.data.citations;
              const output = discriminateOutput(out);
              const policyAnswer = toPlannerPolicyAnswer(out, citations);
              update((s) => {
                const next = { ...s, citations };
                if (output && "clarify" in output && output.clarify) {
                  next.clarifyQuestion = output.question;
                  next.intent = output.intent ?? null;
                } else if (output && "clarify" in output && !output.clarify) {
                  next.shortlist = output.shortlist;
                  next.shortlistReturned = true;
                  next.message = output.message;
                  next.intent = output.intent ?? null;
                  next.tripId = output.trip_id;
                }
                if (policyAnswer) next.policyAnswer = policyAnswer;
                if (!output) {
                  next.resultMessage =
                    asText(out.message) ?? asText(out.answer) ?? null;
                }
                return next;
              });
              return;
            }

            if (frame.event === "done") {
              update((s) => ({ ...s, done: true, streaming: false }));
              return;
            }
          },
          onError: (err) => {
            update((s) => ({ ...s, streaming: false, error: err.message }));
            cancelRef.current = null;
            finalState = stateRef.current;
            settle();
          },
          onComplete: () => {
            update((s) => ({ ...s, streaming: false, done: true }));
            cancelRef.current = null;
            finalState = stateRef.current;
            settle();
          },
        },
      );

      cancelRef.current = () => {
        abort();
        if (!finalState) {
          cancelled = true;
          finalState = { ...stateRef.current, streaming: false };
        }
        settle();
      };

      await settled;
      return { ...(finalState ?? stateRef.current), cancelled };
    },
    [ensureConversation, update],
  );

  const cancel = React.useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    update((s) => ({ ...s, streaming: false }));
  }, [update]);

  const reset = React.useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    update(() => INITIAL_STATE);
  }, [update]);

  return { state, sendMessage, cancel, reset };
}
