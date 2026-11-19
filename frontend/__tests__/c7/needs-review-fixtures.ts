/**
 * C7-07 / C7-08 fixtures — result-frame outputs shaped exactly like the staging
 * captures in the C7 run (verify_C7-07/policy_sse.txt, verify_C7-08/sse_raw.txt)
 * plus the post-fix backend contract (answer populated + needs_review: true).
 */
import type { Citation } from "@/lib/types";

export const MEALS_CITE: Citation = {
  kind: "clause_ref",
  ref: "MEALS-PD",
  detail:
    "Daily meal and incidentals allowance: USD 75 per day for domestic US travel; USD 100 per day for international travel.",
  source: "Atlas Global Travel & Expense Policy",
};

export const MEALS_TEXT =
  "The daily meal and incidentals allowance is USD 75 per day for domestic US travel [Policy §MEALS-PD]. For international travel, the daily meal and incidentals allowance is USD 100 per day [Policy §MEALS-PD].";

/** As captured on staging BEFORE the backend fix: answer null, text only in
 *  answer_structured.full_text, no needs_review flag on the output. The
 *  frontend shows ONLY `answer`, so this renders "No confident answer" (the
 *  backend fix C7-06 is what populates `answer` with the provisional text). */
export const STAGING_NEEDS_REVIEW_OUTPUT: Record<string, unknown> = {
  answer_type: "needs_review",
  answer: null,
  answer_structured: {
    full_text: MEALS_TEXT,
    needs_review: false,
    sentences: [
      {
        text: "The daily meal and incidentals allowance is USD 75 per day for domestic US travel [Policy §MEALS-PD].",
        citations: [MEALS_CITE],
        needs_review: false,
      },
      {
        text: "For international travel, the daily meal and incidentals allowance is USD 100 per day [Policy §MEALS-PD].",
        citations: [MEALS_CITE],
        needs_review: false,
      },
    ],
  },
  asserted: false,
  critique:
    "Critic supported the hypothesis but grounding contract failed (confidence=0.544, citations=1).",
  confidence: 0.5439806494999322,
  confidence_source: "cosine",
  threshold: null,
};

/** Post-fix shared contract: `answer` carries the draft, needs_review true. */
export const CONTRACT_NEEDS_REVIEW_OUTPUT: Record<string, unknown> = {
  ...STAGING_NEEDS_REVIEW_OUTPUT,
  answer: MEALS_TEXT,
  needs_review: true,
};

/** Critic REJECTED the draft (contradicted): the backend withholds it —
 *  answer null, needs_review true — but answer_structured still carries the
 *  full unfiltered (hallucinated) draft. That text must NEVER be rendered. */
export const REJECTED_TEXT =
  "Alcohol is fully reimbursable up to USD 500 per dinner [Policy §ALCOHOL-X].";
export const REJECTED_CITE: Citation = {
  kind: "clause_ref",
  ref: "ALCOHOL-X",
  detail: "Unrelated clause.",
};
export const REJECTED_NEEDS_REVIEW_OUTPUT: Record<string, unknown> = {
  answer_type: "needs_review",
  answer: null,
  answer_structured: {
    full_text: REJECTED_TEXT,
    needs_review: false,
    sentences: [
      { text: REJECTED_TEXT, citations: [REJECTED_CITE], needs_review: false },
    ],
  },
  asserted: false,
  critique: "contradicted",
  confidence: 0.4,
  confidence_source: "cosine",
  threshold: null,
  needs_review: true,
};

/** Provisional answer keeps only the cited, NLI-passing sentence; the dropped
 *  sentence (and its citation) stays only in answer_structured. */
export const PROVISIONAL_KEPT =
  "The daily meal and incidentals allowance is USD 75 per day for domestic US travel [Policy §MEALS-PD].";
export const DROPPED_SENTENCE =
  "Spouses' meals are also covered at the same rate [Policy §SPOUSE-Z].";
export const PARTIAL_PROVISIONAL_OUTPUT: Record<string, unknown> = {
  answer_type: "needs_review",
  answer: PROVISIONAL_KEPT,
  answer_structured: {
    full_text: `${PROVISIONAL_KEPT} ${DROPPED_SENTENCE}`,
    needs_review: true,
    sentences: [
      { text: PROVISIONAL_KEPT, citations: [MEALS_CITE], needs_review: false },
      {
        text: DROPPED_SENTENCE,
        citations: [{ kind: "clause_ref", ref: "SPOUSE-Z" }],
        needs_review: true,
      },
    ],
  },
  asserted: false,
  critique: null,
  confidence: 0.5,
  confidence_source: "cosine",
  threshold: null,
  needs_review: true,
};

/** Contract: no draft at all → answer null and no structured text. */
export const EMPTY_NEEDS_REVIEW_OUTPUT: Record<string, unknown> = {
  answer_type: "needs_review",
  answer: null,
  answer_structured: null,
  asserted: false,
  critique: "Critic rejected the draft.",
  confidence: 0.3,
  confidence_source: "cosine",
  threshold: null,
  needs_review: true,
};

export function sseResult(
  output: Record<string, unknown>,
  citations: Citation[] = [MEALS_CITE],
): string {
  return JSON.stringify({ output, citations });
}

export const SSE_STATUS = JSON.stringify({
  task_type: "policy_grounded_lookup",
  route: "llm",
  confidence: 0.95,
  status: "needs_review",
});

export const SSE_DONE = JSON.stringify({
  needs_review: true,
  message_id: "7495e698-8e6c-44f2-8254-9eb8e719dc8d",
  sequence: 1,
});
