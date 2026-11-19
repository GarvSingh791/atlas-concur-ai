"use client";

import * as React from "react";
import { streamMessages } from "@/lib/api/stream";
import { api } from "@/lib/api/client";
import { parseSSEFrame, discriminateOutput } from "@/lib/planner/sse-events";
import { mapToolActivity } from "@/lib/assistant/tool-activity";
import {
  useAssistantStore,
  type AssistantAnswer,
  type AssistantTurn,
} from "@/stores/assistant-store";
import { resolveGroundingFraction } from "@/lib/assistant/display-confidence";
import { NO_CONFIDENT_ANSWER } from "@/components/planner/policy-answer";
import type {
  Citation,
  Conversation,
  ConversationDetail,
  ConversationMessage,
} from "@/lib/types";

/**
 * Streaming consumer for the reusable AI Assistant (P5-T10).
 *
 * Generalizes the P5-T5 planner pattern onto the same `stream.ts` transport,
 * reusing the generic `parseSSEFrame` / `discriminateOutput` (read-only). The
 * planner is left untouched (Option 2, build-parallel).
 *
 * The client renders the server's truth only — no answer/suggestion/reasoning
 * is generated here. Confidence/route come from the live `status` frame;
 * tool activity is backfilled from the persisted `tool_calls` trace via GET
 * after `done` (the live `tool_activity` frame is not emitted by the current
 * backend, but is still handled defensively).
 */

let turnCounter = 0;
function nextTurnId(prefix: string): string {
  turnCounter += 1;
  return `${prefix}-${turnCounter}-${Date.now()}`;
}

/** Citations come as `Citation[]` (stream) or `{ citations: [] }` (GET). */
function normalizeCitations(raw: unknown): Citation[] {
  if (Array.isArray(raw)) return raw as Citation[];
  if (
    raw &&
    typeof raw === "object" &&
    Array.isArray((raw as { citations?: unknown }).citations)
  ) {
    return (raw as { citations: Citation[] }).citations;
  }
  return [];
}

function asString(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** Extract the calibrated confidence trust fields from the result output. */
function buildTrustPatch(
  output: Record<string, unknown>,
): Partial<AssistantTurn> {
  const patch: Partial<AssistantTurn> = {};

  const calibrated = asNumber(output.confidence);
  const source = asString(output.confidence_source) ?? null;
  const threshold = asNumber(output.threshold);

  if (calibrated !== null) {
    patch.calibratedConfidence = calibrated;
    patch.confidenceSource = source;
    patch.threshold = threshold;
    // For POLICY answers, the grounding bar reflects the CALIBRATED score,
    // remapped through the tau*-normalized display transform so a grounded
    // (above-threshold) answer never renders red. The routing confidence from
    // the status frame is kept separate (turn.routingConfidence).
    const fraction = resolveGroundingFraction(calibrated, source, threshold);
    patch.confidence = Math.round(fraction * 100);
  }

  // Silent-state escalation CTA + near-topic hint.
  const esc = output.escalation;
  if (esc && typeof esc === "object") {
    const e = esc as Record<string, unknown>;
    const message = asString(e.message);
    if (message) {
      patch.escalation = { message, secondary: asString(e.secondary) };
    }
  }
  const nearTopic = asString(output.near_topic);
  if (nearTopic) patch.nearTopic = nearTopic;

  return patch;
}

/** Build the structured answer + plain content from a discriminated result. */
export function buildResultPatch(output: Record<string, unknown>): {
  answer?: AssistantAnswer | null;
  content?: string;
  needsReview?: boolean;
  /** Citations recovered from answer_structured (needs_review drafts). */
  structuredCitations?: Citation[];
} {
  const disc = discriminateOutput(output);

  if (disc && "answer_type" in disc) {
    if (disc.answer_type === "grounded") {
      return {
        answer: {
          answerType: "grounded",
          answer: disc.answer,
          asserted: disc.asserted,
          critique: disc.critique,
        },
      };
    }
    if (disc.answer_type === "silent") {
      return {
        answer: { answerType: "silent", answer: null, reason: disc.reason },
      };
    }
    if (disc.answer_type === "needs_review") {
      // C7-08: surface the drafted, cited text with a caution label — never a
      // blank turn. `answer` is null only when the backend had no draft.
      return {
        answer: {
          answerType: "needs_review",
          answer: disc.answer,
          critique: disc.critique,
          reason: disc.message ?? undefined,
        },
        needsReview: true,
        structuredCitations: disc.citations,
      };
    }
  }

  // Role-bounded refusal (NFR-SEC6): surface the real message.
  if (output.denied === true) {
    return {
      answer: {
        answerType: "denied",
        answer: null,
        reason: typeof output.reason === "string" ? output.reason : undefined,
      },
      needsReview: true,
    };
  }

  // Plain / general response with a message body and no structured answer.
  if (typeof output.message === "string" && output.message) {
    return { content: output.message };
  }
  if (typeof output.answer === "string" && output.answer) {
    return { content: output.answer };
  }
  return {};
}

/**
 * Map a persisted assistant message (GET /conversations/{id}) to a turn.
 *
 * Router-persisted policy replies (C7-09) store `content` = the answer or
 * abstention text and `citations` = { citations, answer_type, needs_review,
 * confidence, confidence_source, answer_structured }. A needs_review reply is
 * rebuilt as a structured needs_review answer so a reload shows the same
 * caution label as the live turn; a withheld draft (content is the backend's
 * "No confident answer" text) maps to answer=null, which PolicyAnswer renders
 * as the explicit no-answer message. Grounded / silent / agent-written replies
 * keep their plain content rendering.
 */
export function hydrateAssistantMessage(m: ConversationMessage): AssistantTurn {
  const steps = m.tool_calls ? mapToolActivity(m.tool_calls) : [];
  const meta: Record<string, unknown> =
    m.citations && typeof m.citations === "object"
      ? (m.citations as unknown as Record<string, unknown>)
      : {};
  const turn: AssistantTurn = {
    id: m.id,
    role: "assistant",
    content: m.content,
    timestamp: m.created_at,
    citations: normalizeCitations(m.citations),
    toolActivity: steps.length > 0 ? steps : undefined,
    // confidence/route are not exposed by GET — honestly omitted.
    confidence: null,
    route: null,
  };

  const calibrated = asNumber(meta.confidence);
  if (calibrated !== null) {
    turn.calibratedConfidence = calibrated;
    turn.confidenceSource = asString(meta.confidence_source) ?? null;
  }

  if (meta.answer_type === "needs_review") {
    const text = typeof m.content === "string" ? m.content.trim() : "";
    const withheld = !text || text === NO_CONFIDENT_ANSWER;
    turn.answer = {
      answerType: "needs_review",
      answer: withheld ? null : m.content,
    };
    // The structured answer carries the text; avoid a duplicate plain bubble.
    turn.content = "";
    turn.needsReview = true;
    if (withheld) turn.citations = [];
  } else if (meta.needs_review === true) {
    turn.needsReview = true;
  }
  return turn;
}

export function useAssistantConversation() {
  const store = useAssistantStore;
  const cancelRef = React.useRef<(() => void) | null>(null);
  const activeAssistantId = React.useRef<string | null>(null);
  const lastUserContent = React.useRef<string | null>(null);

  const ensureConversation = React.useCallback(async (): Promise<string> => {
    const s = store.getState();
    if (s.conversationId) return s.conversationId;
    const body: Record<string, unknown> = { context_type: s.contextType };
    if (s.contextRefId) body.context_ref_id = s.contextRefId;
    const conv = await api.post<Conversation>("/conversations", { body });
    store.getState().setConversationId(conv.id);
    return conv.id;
  }, [store]);

  /** GET the conversation and backfill the assistant turn's persisted trace. */
  const backfillFromServer = React.useCallback(
    async (
      conversationId: string,
      messageId: string | null,
      turnId: string,
    ) => {
      try {
        const detail = await api.get<ConversationDetail>(
          `/conversations/${conversationId}`,
        );
        // Only an assistant message carries a grounding trace. Note: policy_qa
        // / general routes do not persist an assistant message at all (done's
        // message_id is the user message there), so this is best-effort.
        const msg =
          (messageId &&
            detail.messages.find(
              (m) => m.id === messageId && m.role === "assistant",
            )) ||
          [...detail.messages].reverse().find((m) => m.role === "assistant");
        if (!msg) return;
        const patch: Record<string, unknown> = {};
        if (msg.tool_calls) {
          const steps = mapToolActivity(msg.tool_calls);
          if (steps.length > 0) patch.toolActivity = steps;
        }
        const cites = normalizeCitations(msg.citations);
        if (cites.length > 0) patch.citations = cites;
        if (Object.keys(patch).length > 0) {
          store.getState().updateTurn(turnId, patch);
          store.getState().persistToSession();
        }
      } catch {
        // Best-effort enrichment — never breaks the rendered turn.
      }
    },
    [store],
  );

  const sendMessage = React.useCallback(
    async (content: string) => {
      const text = content.trim();
      if (!text) return;

      // Tear down any in-flight stream first.
      if (cancelRef.current) {
        cancelRef.current();
        cancelRef.current = null;
      }

      lastUserContent.current = text;
      const now = new Date().toISOString();
      const userId = nextTurnId("u");
      const assistantId = nextTurnId("a");
      activeAssistantId.current = assistantId;

      const st = store.getState();
      st.setError(null);
      st.addTurn({ id: userId, role: "user", content: text, timestamp: now });
      st.addTurn({
        id: assistantId,
        role: "assistant",
        content: "",
        timestamp: now,
        isStreaming: true,
        citations: [],
        confidence: null,
        route: null,
      });
      st.setStreaming(true);

      let convId: string;
      try {
        convId = await ensureConversation();
      } catch (err) {
        store.getState().updateTurn(assistantId, {
          isStreaming: false,
          error:
            err instanceof Error ? err.message : "Failed to start conversation",
        });
        store.getState().setStreaming(false);
        return;
      }

      let doneMessageId: string | null = null;
      let sawToolActivity = false;

      const cancel = streamMessages(
        convId,
        { content: text, explain_with_llm: true },
        {
          onMessage: () => {},
          onEvent: (event, data) => {
            const frame = parseSSEFrame(event, data);
            if (!frame) return;
            const update = store.getState().updateTurn;

            if (frame.event === "status") {
              // The status frame carries the ROUTING confidence, not the
              // calibrated grounding score. Seed the grounding bar from it as a
              // provisional value; the result frame overrides it with the
              // calibrated (display-transformed) score for policy answers.
              const routing = Math.round((frame.data.confidence ?? 0) * 100);
              update(assistantId, {
                confidence: routing,
                routingConfidence: routing,
                route: frame.data.route || null,
              });
              return;
            }
            if (frame.event === "tool_activity") {
              const steps = mapToolActivity(frame.data.tool_calls);
              if (steps.length > 0) {
                sawToolActivity = true;
                update(assistantId, { toolActivity: steps });
              }
              return;
            }
            if (frame.event === "narrative") {
              const cur = store
                .getState()
                .turns.find((t) => t.id === assistantId);
              const prev = cur?.content ?? "";
              update(assistantId, {
                content: prev ? `${prev} ${frame.data.text}` : frame.data.text,
              });
              return;
            }
            if (frame.event === "result") {
              const { structuredCitations, ...patch } = buildResultPatch(
                frame.data.output,
              );
              const trust = buildTrustPatch(frame.data.output);
              const frameCitations = frame.data.citations ?? [];
              update(assistantId, {
                ...patch,
                ...trust,
                citations:
                  frameCitations.length > 0
                    ? frameCitations
                    : (structuredCitations ?? []),
              });
              return;
            }
            if (frame.event === "done") {
              doneMessageId = frame.data.message_id;
              update(assistantId, {
                isStreaming: false,
                needsReview: frame.data.needs_review,
              });
              return;
            }
          },
          onError: (err) => {
            store.getState().updateTurn(assistantId, {
              isStreaming: false,
              error: err.message || "Streaming failed",
            });
            store.getState().setStreaming(false);
            cancelRef.current = null;
          },
          onComplete: () => {
            store.getState().updateTurn(assistantId, { isStreaming: false });
            store.getState().setStreaming(false);
            cancelRef.current = null;
            store.getState().persistToSession();
            // Backfill the persisted tool trace (live frame not emitted today).
            if (!sawToolActivity) {
              void backfillFromServer(convId, doneMessageId, assistantId);
            }
          },
        },
      );

      cancelRef.current = cancel;
    },
    [ensureConversation, backfillFromServer, store],
  );

  /** Abort an in-flight stream — fails closed and offers retry. */
  const cancel = React.useCallback(() => {
    if (cancelRef.current) {
      cancelRef.current();
      cancelRef.current = null;
    }
    const id = activeAssistantId.current;
    const s = store.getState();
    if (id) {
      const turn = s.turns.find((t) => t.id === id);
      if (turn?.isStreaming) {
        s.updateTurn(id, {
          isStreaming: false,
          error: "Response interrupted.",
        });
      }
    }
    s.setStreaming(false);
  }, [store]);

  const retry = React.useCallback(() => {
    const last = lastUserContent.current;
    if (!last) return;
    // Drop the failed/interrupted assistant turn before retrying.
    const id = activeAssistantId.current;
    if (id) {
      const s = store.getState();
      s.setTurns(s.turns.filter((t) => t.id !== id));
    }
    void sendMessage(last);
  }, [sendMessage, store]);

  /** Rehydrate a conversation from the server (reload / reopen). */
  const loadConversation = React.useCallback(
    async (conversationId: string) => {
      try {
        const detail = await api.get<ConversationDetail>(
          `/conversations/${conversationId}`,
        );
        const turns = detail.messages
          .filter((m) => m.role === "user" || m.role === "assistant")
          .map((m: ConversationMessage) => {
            if (m.role === "user") {
              return {
                id: m.id,
                role: "user" as const,
                content: m.content,
                timestamp: m.created_at,
              };
            }
            return hydrateAssistantMessage(m);
          });
        const s = store.getState();
        s.setConversationId(conversationId);
        s.setTurns(turns);
        s.persistToSession();
      } catch {
        // Leave whatever is already hydrated from sessionStorage.
      }
    },
    [store],
  );

  return { sendMessage, cancel, retry, loadConversation };
}
