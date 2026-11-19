"use client";

import * as React from "react";
import { Plus } from "lucide-react";
import { useSearchParams, useRouter } from "next/navigation";
import { AtlasButton } from "@/components/atlas/button";
import { usePlannerConversation } from "@/lib/planner/use-planner-conversation";
import type { PlannerSendResult } from "@/lib/planner/use-planner-conversation";
import { policyTurnContent } from "@/lib/planner/policy-turn";
import { useTripSelection } from "@/lib/planner/use-trip-selection";
import { usePlannerStore } from "@/stores/planner-store";
import { useConversation } from "@/lib/query/conversation-hooks";
import {
  isIntentEmpty,
  parseIntentSlots,
  toMessagePayload,
} from "@/lib/planner/intent";
import {
  applyPlannerTurn,
  assistantTextForTurn,
} from "@/lib/planner/apply-turn";
import type { PlannerIntent } from "@/lib/types";
import { ConversationPanel } from "./conversation-panel";
import { IntentChips } from "./intent-chips";
import { RecommendationPanel } from "./recommendation-panel";
import { PlannerStepper } from "./planner-stepper";
import { PlannerTripStatus } from "./planner-trip-status";
import type { PlannerTurn } from "@/stores/planner-store";

// ---------------------------------------------------------------------------
// Local turn ID generator — monotonically increasing within the session
// ---------------------------------------------------------------------------

let _turnIdCounter = 0;
function nextTurnId(): string {
  return `turn-${++_turnIdCounter}-${Date.now()}`;
}

// ---------------------------------------------------------------------------
// PlannerScreen
// ---------------------------------------------------------------------------

export function PlannerScreen() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const rawUrlConversationId = searchParams.get("c");
  // "New trip" leaves the old ?c= id in the URL until the router catches up —
  // it must not be re-adopted (or re-hydrated) in the meantime.
  const [abandonedConversationId, setAbandonedConversationId] = React.useState<
    string | null
  >(null);
  const urlConversationId =
    rawUrlConversationId && rawUrlConversationId !== abandonedConversationId
      ? rawUrlConversationId
      : null;

  // ---- Store ------------------------------------------------------------------

  const conversationId = usePlannerStore((s) => s.conversationId);
  const policyConversationId = usePlannerStore((s) => s.policyConversationId);
  const shortlist = usePlannerStore((s) => s.shortlist);
  const mode = usePlannerStore((s) => s.mode);
  const turnCount = usePlannerStore((s) => s.turns.length);
  const selectedOptionId = usePlannerStore((s) => s.selectedOptionId);

  const setConversationId = usePlannerStore((s) => s.setConversationId);
  const setPolicyConversationId = usePlannerStore(
    (s) => s.setPolicyConversationId,
  );
  const addTurn = usePlannerStore((s) => s.addTurn);
  const setIntent = usePlannerStore((s) => s.setIntent);
  const setShortlist = usePlannerStore((s) => s.setShortlist);
  const setNoOptionsMessage = usePlannerStore((s) => s.setNoOptionsMessage);
  const setClarifyQuestion = usePlannerStore((s) => s.setClarifyQuestion);
  const setStreaming = usePlannerStore((s) => s.setStreaming);
  const persistToSession = usePlannerStore((s) => s.persistToSession);
  const hydrateFromSession = usePlannerStore((s) => s.hydrateFromSession);
  const setSelectedTripId = usePlannerStore((s) => s.setSelectedTripId);
  const resetPlanner = usePlannerStore((s) => s.reset);

  // ---- Two SSE conversation hooks ---------------------------------------------
  // One for travel_planning, one for policy_qa. The planner hook is seeded with
  // the URL's ?c= id so that after a reload the next message continues the SAME
  // conversation (the store is only hydrated in an effect, after first render).

  const plannerConv = usePlannerConversation(
    conversationId ?? urlConversationId,
  );
  const policyConv = usePlannerConversation(policyConversationId);

  // ---- Trip selection ---------------------------------------------------------

  const { selectOption, selecting, selectError } = useTripSelection();

  // ---- Server rehydration of text turns on reload with ?c= -------------------

  const { data: rehydratedConv } = useConversation(urlConversationId);

  // ---- On mount: hydrate from URL + sessionStorage ----------------------------

  React.useEffect(() => {
    if (urlConversationId) {
      hydrateFromSession(urlConversationId);
      setConversationId(urlConversationId);
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync text turns (and chips) from server rehydration --------------------
  // Turns: only replay if the shortlist is empty (no live structured state) AND
  // the turn list is empty (avoid duplicates on repeat renders).
  // Chips: when sessionStorage had no intent (e.g. a new tab), restore the
  // intent the agent recorded on its latest turn so chips match the history.

  React.useEffect(() => {
    if (!rehydratedConv) return;
    // UAT-2 Issue 4: the conversation points at its trip — show it after a
    // reload in a new tab (sessionStorage empty) too.
    if (
      !usePlannerStore.getState().selectedTripId &&
      rehydratedConv.context_ref_id
    ) {
      setSelectedTripId(rehydratedConv.context_ref_id);
    }
    if (isIntentEmpty(usePlannerStore.getState().intent)) {
      const lastIntent = [...rehydratedConv.messages]
        .reverse()
        .map((msg) => parseIntentSlots(msg.tool_calls?.intent))
        .find((slots) => slots !== null);
      if (lastIntent) setIntent(lastIntent as Partial<PlannerIntent>);
    }
    if (shortlist.length > 0) return;
    const currentTurns = usePlannerStore.getState().turns;
    if (currentTurns.length > 0) return;

    rehydratedConv.messages.forEach((msg) => {
      addTurn({
        id: nextTurnId(),
        role: msg.role as "user" | "assistant",
        content: msg.content,
        timestamp: msg.created_at,
      });
    });
  }, [rehydratedConv]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync planner conv ID → store → URL param ------------------------------

  React.useEffect(() => {
    const convId = plannerConv.state.conversationId;
    if (convId && convId !== conversationId) {
      setConversationId(convId);
      router.replace(`/planner?c=${convId}`, { scroll: false });
    }
  }, [plannerConv.state.conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync policy conv ID → store -------------------------------------------

  React.useEffect(() => {
    const convId = policyConv.state.conversationId;
    if (convId && convId !== policyConversationId) {
      setPolicyConversationId(convId);
    }
  }, [policyConv.state.conversationId]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync shortlist from planner conv state → store ------------------------

  React.useEffect(() => {
    const { shortlist: sl, citations } = plannerConv.state;
    if (sl.length > 0) {
      setShortlist(sl, citations);
      const activeConvId = usePlannerStore.getState().conversationId;
      if (activeConvId) persistToSession(activeConvId);
    }
  }, [plannerConv.state.shortlist]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync clarify question → store -----------------------------------------

  React.useEffect(() => {
    setClarifyQuestion(plannerConv.state.clarifyQuestion);
  }, [plannerConv.state.clarifyQuestion]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Sync streaming flag → store -------------------------------------------

  React.useEffect(() => {
    setStreaming(plannerConv.state.streaming);
  }, [plannerConv.state.streaming]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- Finished plan-mode turn → store + ONE assistant message ---------------
  // The server's resolved intent fills the chips; a returned shortlist (even an
  // empty one, with the server's reason) replaces the old one; the clarify
  // question is shown once, as this turn's assistant message.

  const finishPlannerTurn = React.useCallback(
    (res: PlannerSendResult) => {
      if (res.cancelled) return;
      applyPlannerTurn(res, { setIntent, setShortlist, setNoOptionsMessage });

      const content = assistantTextForTurn(res);
      if (content) {
        addTurn({
          id: nextTurnId(),
          role: "assistant",
          content,
          timestamp: new Date().toISOString(),
        });
      }

      // Persist to sessionStorage after every planner response
      const activeConvId =
        usePlannerStore.getState().conversationId ?? res.conversationId;
      if (activeConvId) persistToSession(activeConvId);
    },
    [addTurn, setIntent, setShortlist, setNoOptionsMessage, persistToSession],
  );

  // ---- Send message handler ---------------------------------------------------

  const handleSendMessage = React.useCallback(
    async (text: string) => {
      const userTurn: PlannerTurn = {
        id: nextTurnId(),
        role: "user",
        content: text,
        timestamp: new Date().toISOString(),
      };
      addTurn(userTurn);

      if (mode === "policy") {
        // Policy Q&A mode. C7-07: sendMessage now resolves with the FINAL state
        // of this stream (it used to resolve before the stream ran, and the
        // `policyConv.state` read here was a stale render closure), and the
        // turn is built from the real answer — never a "received" placeholder.
        const res = await policyConv.sendMessage(
          { content: text },
          "policy_qa",
        );
        if (res.cancelled) return;
        addTurn({
          id: nextTurnId(),
          role: "assistant",
          ...policyTurnContent(res),
          timestamp: new Date().toISOString(),
        });
        return;
      }

      // Plan mode: send the message text with the current chips; the agent
      // extracts slots from the text and merges them onto the chips. Read the
      // chips from the store (not a render closure) so a just-saved chip counts.
      const { intent, conversationId: activeConvId } =
        usePlannerStore.getState();
      const payload = toMessagePayload(intent, {
        content: text,
        conversationId: activeConvId ?? undefined,
      });

      // Read the FINAL state of this stream from the resolved result — not the
      // stale `plannerConv.state` closure (same fault as C7-07).
      const res = await plannerConv.sendMessage(payload);
      finishPlannerTurn(res);
    },
    [mode, addTurn, plannerConv, policyConv, finishPlannerTurn],
  );

  // ---- Intent chip change handler --------------------------------------------
  // Called when the user edits a chip; re-sends the current intent to the agent
  // (source=chips: the server skips text extraction for a structured edit).

  const handleIntentChange = React.useCallback(async () => {
    const currentIntent = usePlannerStore.getState().intent;
    const convId = usePlannerStore.getState().conversationId;
    const payload = toMessagePayload(currentIntent, {
      content: "Update my trip plan",
      conversationId: convId ?? undefined,
      source: "chips",
    });
    const res = await plannerConv.sendMessage(payload);
    finishPlannerTurn(res);
  }, [plannerConv, finishPlannerTurn]);

  // ---- New trip ----------------------------------------------------------------
  // UAT-2 Issue 4: one planning conversation = one draft trip. A second,
  // separate trip starts from a fresh conversation (cleared chips + history).

  const handleNewTrip = React.useCallback(() => {
    const { conversationId: current, policyConversationId: policyId } =
      usePlannerStore.getState();
    plannerConv.reset();
    resetPlanner();
    if (policyId) setPolicyConversationId(policyId);
    setAbandonedConversationId(current ?? rawUrlConversationId);
    router.replace("/planner", { scroll: false });
  }, [
    plannerConv,
    resetPlanner,
    setPolicyConversationId,
    rawUrlConversationId,
    router,
  ]);

  // ---- Derived state ---------------------------------------------------------

  // Disable input while EITHER stream (plan or policy) is in flight.
  const streaming = plannerConv.state.streaming || policyConv.state.streaming;
  const error = plannerConv.state.error;

  // ---- Render ----------------------------------------------------------------

  // Layout: at xl+ the planner fills <main> and the chat / recommendations sit
  // side by side, each scrolling on its own. Below xl nothing is height-locked:
  // the column (header, trip card, chips, chat, recommendations) scrolls as one
  // page inside <main>, the chat keeps a fixed usable height, and the
  // recommendations grow with their cards — a tall trip card can no longer
  // squeeze them to 0px (UAT-2: 1222x686, hotel cards unreachable).
  return (
    <div
      data-planner-layout="root"
      className="flex flex-col xl:h-full xl:overflow-hidden"
    >
      {/* Stepper row */}
      <div className="shrink-0 border-b border-[#E2E8F0] bg-white py-[var(--space-3)]">
        <div className="flex items-start gap-2">
          <PlannerStepper className="min-w-0 flex-1" />
          {(conversationId || turnCount > 0 || shortlist.length > 0) && (
            <AtlasButton
              variant="secondary"
              size="sm"
              className="mr-[var(--space-4)] shrink-0"
              onClick={handleNewTrip}
              disabled={streaming || selecting}
            >
              <Plus aria-hidden="true" />
              New trip
            </AtlasButton>
          )}
        </div>
        <PlannerTripStatus
          className="mx-[var(--space-4)] mt-[var(--space-3)]"
          selecting={selecting}
        />
      </div>

      {/* Intent chips row */}
      <div className="shrink-0 border-b border-[#E2E8F0] bg-[#FAFAFA]">
        <IntentChips onIntentChange={handleIntentChange} />
      </div>

      {/* Two-panel body — stacked below xl, side by side at xl+ */}
      <div
        data-planner-layout="body"
        className="flex flex-col xl:flex-1 xl:min-h-0 xl:flex-row xl:overflow-hidden"
      >
        {/* Conversation panel — fixed 45vh (min 20rem) below xl, ~40% at xl+ */}
        <div
          data-planner-layout="conversation"
          className="flex h-[45vh] min-h-[20rem] shrink-0 overflow-hidden border-b border-[#E2E8F0] xl:h-auto xl:min-h-0 xl:w-[40%] xl:border-b-0 xl:border-r"
        >
          <ConversationPanel
            onSendMessage={handleSendMessage}
            streaming={streaming}
            className="flex-1"
          />
        </div>

        {/* Recommendation panel — natural height below xl (never < 16rem),
            the remaining ~60% at xl+ with its own scroll */}
        <div
          data-planner-layout="recommendations"
          className="flex min-h-[16rem] flex-col xl:min-h-0 xl:flex-1 xl:overflow-hidden"
        >
          <RecommendationPanel
            onSelectOption={selectOption}
            selectingId={selecting ? selectedOptionId : null}
            error={selectError ?? error ?? null}
            className="flex-1 xl:min-h-0"
          />
        </div>
      </div>
    </div>
  );
}
