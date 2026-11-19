"use client";

import * as React from "react";
import { AlertTriangle, LifeBuoy } from "lucide-react";
import { CitationChip } from "@/components/atlas/citation-chip";
import type { Citation } from "@/lib/types";
import { cn } from "@/lib/utils";

interface PolicyAnswerProps {
  answerType: "grounded" | "silent" | "needs_review";
  answer: string | null;
  asserted?: string;
  critique?: string | null;
  reason?: string;
  citations: Citation[];
  /** Silent-state escalation CTA (from result.output.escalation). */
  escalation?: { message: string; secondary?: string } | null;
  /** Silent-state near-topic hint (from result.output.near_topic). */
  nearTopic?: string | null;
  className?: string;
}

/** Caution label shown on a low-confidence (needs_review) policy answer that has text. */
export const NEEDS_REVIEW_LABEL =
  "Needs review — low confidence; verify against the cited policy clause";

/**
 * Caution label for a needs_review answer whose text was withheld. It carries
 * no sources (the presentation boundary strips them), so it must not point the
 * user at a "cited policy clause" that is not on screen (C7R).
 */
export const NEEDS_REVIEW_WITHHELD_LABEL =
  "Needs review — low confidence; no answer could be verified against the policy";

/** Explicit message when a needs_review answer carries no drafted text. */
export const NO_CONFIDENT_ANSWER =
  "No confident answer — please check the policy or ask finance.";

/** Copy for the abstention reason codes the Policy Agent emits (C7R). */
const SILENT_REASON_COPY: Record<string, string> = {
  model_declined: "The policy documents do not address this question.",
  no_verified_citation: "No policy clause answers this question directly.",
};

/** Internal reason codes look like `snake_case` or `snake_case: detail`. */
const REASON_CODE = /^[a-z][a-z0-9_]*(?::.*)?$/;

/**
 * C7R: the agent's `reason` is an internal code ("model_declined",
 * "below_threshold: max_sim=0.12 …") recorded for audit. Users see readable
 * copy for the codes that have one and nothing for the rest; a reason that is
 * already plain prose is shown as-is.
 */
export function silentReasonText(reason: string | null | undefined): string {
  const raw = (reason ?? "").trim();
  if (!raw) return "";
  const code = raw.split(":")[0]?.trim() ?? "";
  if (SILENT_REASON_COPY[code]) return SILENT_REASON_COPY[code];
  return REASON_CODE.test(raw) ? "" : raw;
}

export function PolicyAnswer({
  answerType,
  answer,
  asserted,
  critique,
  reason,
  citations,
  escalation,
  nearTopic,
  className,
}: PolicyAnswerProps) {
  return (
    <div
      className={cn(
        "rounded-atlas-lg p-[var(--space-4)] border",
        answerType === "grounded"
          ? "bg-[var(--color-ai-surface)] border-[#DDD6FE]"
          : "bg-[#F8FAFC] border-[#E2E8F0]",
        className,
      )}
    >
      {answerType === "silent" && (
        <div className="flex flex-col gap-2">
          <p className="text-[length:var(--text-body-size)] text-[#64748B] italic">
            Policy is silent on this.
            {silentReasonText(reason) ? ` ${silentReasonText(reason)}` : ""}
          </p>
          {nearTopic ? (
            <p className="text-[length:var(--text-caption-size)] text-[#64748B]">
              Closest related topic: {nearTopic}
            </p>
          ) : null}
          {escalation?.message ? (
            <div className="mt-1 flex items-start gap-2 rounded-atlas-md border border-[#DDD6FE] bg-[var(--color-ai-surface)] p-3">
              <LifeBuoy
                className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-ai-primary)]"
                aria-hidden="true"
              />
              <div className="flex flex-col gap-0.5">
                <button
                  type="button"
                  className="w-fit text-left text-[length:var(--text-caption-size)] font-medium text-[var(--color-ai-primary)] hover:underline focus-visible:outline-none focus-visible:ring-[length:var(--focus-ring-width)] focus-visible:ring-[var(--focus-ring-color)] focus-visible:ring-offset-[length:var(--focus-ring-offset)] rounded-atlas-sm"
                >
                  {escalation.message}
                </button>
                {escalation.secondary ? (
                  <span className="text-[length:var(--text-caption-size)] text-[#64748B]">
                    {escalation.secondary}
                  </span>
                ) : null}
              </div>
            </div>
          ) : null}
        </div>
      )}
      {answerType === "needs_review" && (
        <div className="flex flex-col gap-2" data-testid="policy-needs-review">
          {/* C7-07/08: always a visible caution label + either the drafted,
              cited text or an explicit no-answer message — never blank. */}
          <p
            role="note"
            className="flex items-start gap-1.5 text-[length:var(--text-caption-size)] font-medium text-[#92400E]"
          >
            <AlertTriangle
              className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--color-warning)]"
              aria-hidden="true"
            />
            {answer && answer.trim()
              ? NEEDS_REVIEW_LABEL
              : NEEDS_REVIEW_WITHHELD_LABEL}
          </p>
          {answer && answer.trim() ? (
            <p className="text-[length:var(--text-body-size)] text-[#1E293B]">
              {answer}
            </p>
          ) : (
            <p className="text-[length:var(--text-body-size)] text-[#64748B] italic">
              {NO_CONFIDENT_ANSWER}
            </p>
          )}
        </div>
      )}
      {answerType === "grounded" && answer && (
        <>
          <p className="text-[length:var(--text-body-size)] text-[#1E293B]">
            {answer}
          </p>
          {asserted && (
            <p className="mt-2 text-[length:var(--text-caption-size)] text-[var(--color-ai-primary)] font-medium">
              {asserted}
            </p>
          )}
          {critique && (
            <p className="mt-1 text-[length:var(--text-caption-size)] text-[#64748B]">
              {critique}
            </p>
          )}
        </>
      )}
      {citations.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {citations.map((c, i) => (
            <CitationChip
              key={i}
              clauseId={c.ref}
              documentType={c.document_type}
              detail={c.detail}
            />
          ))}
        </div>
      )}
    </div>
  );
}
