import type { PlannerSendResult } from "./use-planner-conversation";
import type { PlannerTurn } from "@/stores/planner-store";

// ---------------------------------------------------------------------------
// C7-07: build the assistant turn for an Ask Policy response
// ---------------------------------------------------------------------------

export function policyTurnContent(
  res: Pick<
    PlannerSendResult,
    | "error"
    | "clarifyQuestion"
    | "policyAnswer"
    | "resultMessage"
    | "streamingText"
  >,
): Pick<PlannerTurn, "content" | "policyAnswer"> {
  if (res.error) return { content: `Error: ${res.error}` };
  if (res.clarifyQuestion) return { content: res.clarifyQuestion };
  const pa = res.policyAnswer;
  if (pa) {
    // `content` is the plain-text fallback (session persistence); the
    // structured answer is what the conversation panel renders.
    const content =
      pa.answer ??
      (pa.answerType === "silent"
        ? `Policy is silent on this.${pa.reason ? ` ${pa.reason}` : ""}`
        : "No confident answer — please check the policy or ask finance.");
    return { content, policyAnswer: pa };
  }
  const plain = res.resultMessage || res.streamingText;
  if (plain) return { content: plain };
  // Never a generic "received" placeholder: say plainly that nothing came back.
  return {
    content: "No answer was returned — please check the policy or ask finance.",
  };
}
