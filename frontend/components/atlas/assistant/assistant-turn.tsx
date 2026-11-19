"use client";

import * as React from "react";
import { AlertTriangle, RefreshCw, ShieldAlert } from "lucide-react";
import { MessageBubble } from "@/components/planner/message-bubble";
import { PolicyAnswer } from "@/components/planner/policy-answer";
import { ConfidenceIndicator } from "@/components/atlas/confidence-indicator";
import { AIReasoningPanel } from "@/components/atlas/ai-reasoning-panel";
import { ToolActivityIndicator } from "./tool-activity-indicator";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  TooltipProvider,
} from "@/components/ui/tooltip";
import type { AssistantTurn as Turn } from "@/stores/assistant-store";

/**
 * Renders a single assistant/user turn (P5-T10). Composes the P5-T1 primitives
 * and the reused P5-T5 conversation pieces (MessageBubble, PolicyAnswer) — no
 * new design-system primitives. All content is the server's truth.
 */

export interface AssistantTurnProps {
  turn: Turn;
  onRetry: () => void;
}

/** Honest reasoning trace — route is real (status frame); ai_decision_logs is
 *  not exposed, so we never fabricate step-by-step reasoning. */
function buildReasoning(turn: Turn): string[] {
  const steps: string[] = [];
  if (turn.route === "deterministic") {
    steps.push("Resolved by the deterministic policy engine (no LLM).");
  } else if (turn.route === "llm") {
    steps.push("Resolved with LLM reasoning over retrieved policy.");
  }
  return steps;
}

/** Policy-surface grounding confidence: the UNCHANGED ConfidenceIndicator bar
 *  (already fed the tau*-normalized display value upstream) relabelled
 *  "Grounding confidence", with a tooltip exposing the RAW calibrated score +
 *  tau*. We never modify ConfidenceIndicator or its banding (shared with OCR). */
function GroundingConfidence({ turn }: { turn: Turn }) {
  const value = turn.confidence as number;
  const hasCalibrated =
    typeof turn.calibratedConfidence === "number" &&
    turn.confidenceSource === "calibrated";
  const raw = turn.calibratedConfidence;
  const tau = turn.threshold;

  const trigger = (
    <span className="inline-flex items-center gap-2">
      <span className="text-[length:var(--text-caption-size)] text-[#64748B] whitespace-nowrap">
        Grounding confidence
      </span>
      <ConfidenceIndicator value={value} showLabel={false} />
      <span className="text-[length:var(--text-caption-size)] text-[#64748B]">
        {Math.max(0, Math.min(100, Math.round(value)))}%
      </span>
    </span>
  );

  if (!hasCalibrated || typeof raw !== "number") {
    return <div className="w-fit">{trigger}</div>;
  }

  return (
    <TooltipProvider delayDuration={150}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="w-fit rounded-atlas-sm focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)]"
            aria-label="About grounding confidence"
          >
            {trigger}
          </button>
        </TooltipTrigger>
        <TooltipContent
          role="tooltip"
          className="max-w-[300px] whitespace-normal text-[#475569]"
        >
          <span className="block font-medium">
            Calibrated relevance: {raw.toFixed(4)}
          </span>
          {typeof tau === "number" ? (
            <span className="block">Threshold tau* {tau.toFixed(6)}</span>
          ) : null}
          <span className="mt-1 block text-[#64748B]">
            Answers below the calibrated relevance threshold are withheld as
            &ldquo;policy silent.&rdquo;
          </span>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function AssistantTurn({ turn, onRetry }: AssistantTurnProps) {
  if (turn.role === "user") {
    return (
      <MessageBubble
        role="user"
        content={turn.content}
        timestamp={turn.timestamp}
      />
    );
  }

  const { answer } = turn;
  const showTyping =
    turn.isStreaming && !turn.content && !answer && !turn.error;
  const reasoning = buildReasoning(turn);
  const showConfidence =
    !turn.isStreaming &&
    turn.route === "llm" &&
    typeof turn.confidence === "number";
  const showDeterministic = !turn.isStreaming && turn.route === "deterministic";

  return (
    <div className="flex w-full flex-col gap-2">
      {/* Tool activity (real persisted trace; generic "Working…" while streaming) */}
      {(turn.toolActivity?.length || turn.isStreaming) && !turn.error ? (
        <ToolActivityIndicator
          steps={turn.toolActivity ?? []}
          streaming={turn.isStreaming}
        />
      ) : null}

      {/* Error / interrupted — fail closed with retry */}
      {turn.error ? (
        <div className="rounded-atlas-md border border-[#FECACA] bg-[#FEF2F2] p-3">
          <div className="flex items-start gap-2">
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-error)]"
              aria-hidden="true"
            />
            <div className="flex-1">
              <p className="text-[length:var(--text-body-size)] text-[#991B1B]">
                {turn.error}
              </p>
              <button
                type="button"
                onClick={onRetry}
                className="mt-2 inline-flex items-center gap-1.5 text-[length:var(--text-caption-size)] font-medium text-[var(--color-ai-primary)] hover:underline focus-visible:atlas-focus-ring rounded-atlas-sm"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
                Retry
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {/* Typing indicator before any tokens arrive */}
      {showTyping ? (
        <MessageBubble
          role="assistant"
          content=""
          timestamp={turn.timestamp}
          isStreaming
        />
      ) : null}

      {/* Streamed narrative / plain content */}
      {turn.content && !turn.error ? (
        <MessageBubble
          role="assistant"
          content={turn.content}
          timestamp={turn.timestamp}
        />
      ) : null}

      {/* Structured policy answer (grounded / silent / needs_review). C7-08:
          a low-confidence needs_review answer renders its drafted, cited text
          with a caution label (or an explicit no-answer message) — never blank. */}
      {answer &&
      (answer.answerType === "grounded" ||
        answer.answerType === "silent" ||
        answer.answerType === "needs_review") ? (
        <PolicyAnswer
          answerType={answer.answerType}
          answer={answer.answer}
          asserted={answer.asserted}
          critique={answer.critique}
          reason={answer.reason}
          citations={turn.citations ?? []}
          escalation={turn.escalation}
          nearTopic={turn.nearTopic}
        />
      ) : null}

      {/* Role-bounded refusal (NFR-SEC6) — clear, not opaque */}
      {answer && answer.answerType === "denied" ? (
        <div className="rounded-atlas-md border border-[#FDE68A] bg-[#FFFBEB] p-3">
          <div className="flex items-start gap-2">
            <ShieldAlert
              className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-warning)]"
              aria-hidden="true"
            />
            <div>
              <p className="text-[length:var(--text-body-size)] font-medium text-[#92400E]">
                I can&apos;t complete that for your role.
              </p>
              <p className="mt-1 text-[length:var(--text-caption-size)] text-[#92400E]">
                {answer.reason ||
                  "This action needs review or a permission your account doesn't have."}
              </p>
            </div>
          </div>
        </div>
      ) : null}

      {/* Trust cues — only from real data, never fabricated */}
      {showConfidence ? <GroundingConfidence turn={turn} /> : null}
      {showDeterministic ? (
        <span className="inline-flex w-fit items-center rounded-atlas-full bg-[var(--color-ai-surface)] px-2.5 py-0.5 text-[length:var(--text-caption-size)] font-medium text-[var(--color-ai-primary)]">
          Deterministic
        </span>
      ) : null}

      {!turn.isStreaming && reasoning.length > 0 ? (
        <AIReasoningPanel title="Why this answer?" steps={reasoning} />
      ) : null}
    </div>
  );
}
