"""TravelAgent — deterministic-first trip planning agent (P4-T9, FR-T1..T7).

Implements the Agent protocol (P4-T7 contracts.py).
Registered as AiAgent.travel in the AgentRegistry by register_travel().

Pipeline (ReAct loop, deterministic-first):
  1. Parse intent + clarify missing slots   (FR-T1/T2) — deterministic
  2. Role-bound check (plan for self only)  (NFR-SEC6) — deterministic
  3. Retrieve from InventoryTool            (FR-T3)    — deterministic
  4. Policy-evaluate per candidate          (FR-T5)    — deterministic
  5. Rank by objectives                     (FR-T4)    — deterministic
  6. Reflect (TravelPolicyHonestyCritic)   (FR-A3)    — deterministic
  7. Optional LLM narrative                 (FR-T6)    — llm (gated by flag)
  8. Persist on selection                   (FR-T7)    — deterministic
  9. Append ConversationMessage (if conv_id present)
 10. Write decision log                     (M-R4)     — always

Step 0 (extraction.py) merges the conversation's prior intent, the payload chips
and the slots stated in the message text. The text is read by a deterministic
parser, plus — with a real provider only — an LLM extractor whose JSON output is
re-validated and falls back to the parser on any failure. Ranking only ever sees
validated slot values.

Beyond extraction, the LLM is invoked ONLY for step 7 when explain_with_llm=True.
On ProviderError the agent falls back to templated explanations and marks
status=needs_review (NFR-R1).  The reflection loop is entirely deterministic
and never degrades due to LLM availability.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.travel_agent.extraction import resolve_slots
from ai_agents.agents.travel_agent.intent import (
    TripIntent,
    intent_slots,
    missing_slot_question,
    parse_intent,
)
from ai_agents.agents.travel_agent.narrative import enrich_with_narratives
from ai_agents.agents.travel_agent.persistence import (
    SelectionUnavailable,
    TripSelectionResult,
    append_assistant_message,
    apply_trip_selection,
    load_prior_intent,
    trip_label,
)
from ai_agents.agents.travel_agent.ranking import RankedOption, retrieve_and_rank
from ai_agents.agents.travel_agent.reflection import (
    TravelPolicyHonestyCritic,
    TravelShortlistHypothesizer,
)
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.guardrails.actions import AgentAction, check_action
from ai_agents.guardrails.reflection import ReflectionResult, reflect
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.profile import ProfileIntent, ProfileTool
from database.models.ai_decision_log import AiAgent, AiRoute
from services.exceptions import ProviderError
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmProvider
from services.mock_inventory.locations import AIRPORT_CITIES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization helper
# ---------------------------------------------------------------------------


def _option_to_dict(opt: RankedOption, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "option_id": opt.option_id,
        "segment_type": opt.segment_type,
        "supplier": opt.supplier,
        "amount_usd": opt.amount_usd,
        "sustainability_score": opt.sustainability_score,
        "cabin_class": opt.cabin_class,
        "duration_hours": opt.duration_hours,
        "is_direct": opt.is_direct,
        "city": opt.city,
        "nightly_rate_usd": opt.nightly_rate_usd,
        "room_type": opt.room_type,
        "nights": opt.nights,
        "stay_total_usd": opt.stay_total_usd,
        "in_policy": opt.in_policy,
        "policy_label": "In Policy" if opt.in_policy else "Out of Policy",
        "policy_reason": opt.policy_reason,
        "clause_refs": list(opt.clause_refs),
        "requires_approver": opt.requires_approver,
        "explanation": opt.explanation,
        # Product gap 2 stage 2: PTA-02 / AIR-06 / AIR-04 notices shown on the option.
        "lowest_fare_usd": opt.lowest_fare_usd,
        "policy_notices": [dict(n) for n in opt.policy_notices],
    }


def _build_message_content(shortlist: list[RankedOption], trip_message: str | None) -> str:
    """Assistant reply: the trip outcome on a selection turn, else the shortlist."""
    if trip_message:
        return trip_message
    lines = [f"I found {len(shortlist)} travel option(s) for your trip:"]
    for i, opt in enumerate(shortlist, 1):
        label = "In Policy" if opt.in_policy else "Out of Policy"
        lines.append(f"{i}. [{label}] {opt.supplier} — ${opt.amount_usd:.0f} — {opt.explanation}")
    return "\n".join(lines)


_STATUS_WORDS: dict[str, str] = {
    "pending_approval": "awaiting approval",
    "approved": "approved",
    "rejected": "rejected",
    "completed": "completed",
    "cancelled": "cancelled",
    "returned": "returned for changes",
}


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "not priced"


def _leg_phrase(opt: RankedOption, nights: int | None) -> str:
    if opt.segment_type == "hotel":
        rate = opt.nightly_rate_usd if opt.nightly_rate_usd is not None else opt.amount_usd
        n = nights or 1
        total = float(rate) * n
        return f"the {opt.supplier} hotel ({n} night{'s' if n != 1 else ''}, {_money(total)})"
    return f"the {opt.supplier} flight ({_money(opt.amount_usd)})"


def _policy_sentences(result: TripSelectionResult, selected: list[RankedOption]) -> str:
    """Product gap 2 stage 2: the process-rule notices of the selection, clause-cited —
    PTA-02 ("booked N days ahead, policy requires M"), AIR-06, AIR-04 per selected
    option, then PTA-05 when the selection changes an approved / under-review trip."""
    parts: list[str] = []
    for opt in selected:
        for n in opt.policy_notices:
            message = str(n.get("message") or "").strip()
            if message and message not in parts:
                parts.append(f"{n.get('clause_ref')}: {message}")
    if result.reapproval is not None:
        parts.append(f"{result.reapproval.clause_ref}: {result.reapproval.reason}")
    elif result.changes_trip_id is not None and result.reapproval_checked:
        parts.append(
            "PTA-05: this change keeps the estimated cost within the re-approval threshold "
            "of the approved trip; it still needs approval before booking (PTA-01)."
        )
    return " ".join(f"Policy notice — {p}" for p in parts)


def _trip_messages(
    result: TripSelectionResult, selected: list[RankedOption]
) -> tuple[str, str | None]:
    """(reply sentence, notice) describing what the selection did to the trip."""
    message, notice = _trip_messages_base(result, selected)
    policy = _policy_sentences(result, selected) if result.action != "unchanged" else ""
    return (f"{message} {policy}" if policy else message), notice


def _trip_messages_base(
    result: TripSelectionResult, selected: list[RankedOption]
) -> tuple[str, str | None]:
    trip = result.trip
    label = trip_label(trip.origin, trip.primary_destination, trip.start_date, trip.end_date)
    cost = _money(trip.estimated_cost) if trip.estimated_cost is not None else None
    hotel_nights = next(
        (s.get("nights") for s in result.segments if s.get("segment_type") == "hotel"), None
    )
    by_type = {o.segment_type: o for o in selected}
    notice: str | None = None

    if result.action == "unchanged":
        return f"That option is already on your draft trip {label}.", None

    if result.action == "created":
        legs = " and ".join(_leg_phrase(o, hotel_nights) for o in selected)
        prev = result.previous_trip
        if prev is not None:
            if prev.get("reason") == "not_draft":
                state = _STATUS_WORDS.get(str(prev.get("status")), str(prev.get("status")))
                notice = (
                    f"Started a new draft trip {label} — your earlier trip {prev.get('label')} "
                    f"is {state}, so it can't be changed. It is still in Trips."
                )
                if prev.get("status") == "returned":
                    notice = (
                        f"Started a new draft trip {label} — your earlier trip "
                        f"{prev.get('label')} was returned for changes, so the planner "
                        "won't change it. It is still in Trips, where you can review the "
                        "approver's comment and resubmit it."
                    )
                if prev.get("status") == "rejected":
                    notice += (
                        " Because the earlier trip was rejected, this one will go to a reviewer."
                    )
            else:
                notice = (
                    f"Started a new draft trip {label} — your earlier draft "
                    f"{prev.get('label')} is still in Trips."
                )
            message = f"{notice} Saved {legs} to it."
        else:
            message = f"Saved {legs} to a new draft trip {label}."
        if cost:
            message += f" Estimated cost {cost}."
        return message, notice

    parts: list[str] = []
    if result.end_date_change is not None:
        # UAT2 defect E: only the return date changed -> this same draft, in place.
        old_end, new_end = result.end_date_change
        parts.append(
            f"Updated the return date of your draft trip {label} from "
            f"{old_end.isoformat()} to {new_end.isoformat()}."
        )
    for seg_type in result.added:
        opt = by_type.get(seg_type)
        if opt is not None:
            parts.append(f"Added {_leg_phrase(opt, hotel_nights)} to your draft trip {label}.")
    for seg_type in result.replaced:
        opt = by_type.get(seg_type)
        if opt is not None:
            parts.append(
                f"Replaced the {seg_type} on your draft trip {label} with "
                f"{_leg_phrase(opt, hotel_nights)}."
            )
    if result.stale_hotel:
        parts.append(_stale_hotel_sentence(result.segments))
    message = " ".join(parts) or f"Updated your draft trip {label}."
    if cost:
        message += f" Estimated cost is now {cost}."
    return message, None


def _stale_hotel_sentence(segments: tuple[dict[str, Any], ...]) -> str:
    """The kept hotel leg still covers the OLD stay — say so (never re-priced silently)."""
    hotel = next((s for s in segments if s.get("segment_type") == "hotel"), {})
    name = f"The {hotel['supplier']} hotel" if hotel.get("supplier") else "The hotel"
    check_in, check_out = hotel.get("check_in_date"), hotel.get("check_out_date")
    nights = hotel.get("nights")
    if check_in and check_out:
        span = f"{check_in} – {check_out}"
        if isinstance(nights, int):
            span += f" ({nights} night{'s' if nights != 1 else ''})"
    else:
        span = "the old dates"
    return f"{name} on it still covers {span} — select a hotel to cover the new dates."


def _trip_change(result: TripSelectionResult) -> dict[str, Any]:
    trip = result.trip
    end_change = result.end_date_change
    return {
        "action": result.action,
        "segment_types_added": list(result.added),
        "segment_types_replaced": list(result.replaced),
        "previous_trip": dict(result.previous_trip) if result.previous_trip else None,
        "estimated_cost": str(trip.estimated_cost) if trip.estimated_cost is not None else None,
        "segment_count": len(result.segments),
        # UAT2 defect E: only the return date changed and the draft was updated in place.
        "end_date_change": (
            {"from": end_change[0].isoformat(), "to": end_change[1].isoformat()}
            if end_change is not None
            else None
        ),
        "stale_hotel": result.stale_hotel,
        # Product gap 2 stage 2 (PTA-05): the approved / under-review trip this draft
        # changes, and whether the change needs re-approval (> 10% cost increase).
        "changes_trip_id": result.changes_trip_id,
        "reapproval_required": result.reapproval is not None
        and result.reapproval.flag_type == "approval_required",
        # Internal audit snapshots — the router records them and strips them from the
        # streamed frame.
        "audit": {"before": result.before, "after": result.after},
    }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TravelAgent:
    """Trip planning agent implementing the Agent protocol (P4-T7 contracts.py).

    Deterministic-first: the full retrieve→evaluate→rank→reflect pipeline runs
    without calling the LLM.  The LLM is used ONLY for an optional narrative
    enrichment step (FR-T6), gated by payload["explain_with_llm"].
    """

    agent_type: AiAgent = AiAgent.travel

    def __init__(
        self,
        provider: LlmProvider | None = None,
        *,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._provider: LlmProvider = provider or get_llm_provider()
        # Injected clock for relative dates ("next Tuesday"); tests pin it.
        self._today: Callable[[], date] = today or date.today

    def _resolve_payload(
        self, payload: dict[str, Any], *, session: Session
    ) -> tuple[dict[str, Any], str, str | None]:
        """Merge prior-turn intent < payload chips < the message text (in place).

        Returns (payload, extraction_source, date_notice). A payload with no
        message text and no conversation history passes through untouched (legacy
        chip/API path). ``date_notice`` is set when the message's dates were
        rejected and the prior ones kept (UAT2 defect K) — the turn asks it.
        """
        text = payload.get("question")
        if not isinstance(text, str) or payload.get("selected_option_ids"):
            text = None  # a selection re-runs the plan; it never re-reads prose
        if payload.get("source") == "chips":
            text = None  # chip edits are already structured

        prior: dict[str, Any] | None = None
        conv_id = payload.get("conversation_id")
        if conv_id:
            try:
                prior = load_prior_intent(conversation_id=str(conv_id), session=session)
            except Exception as exc:  # history is an optimisation, never fatal
                logger.warning("TravelAgent: could not load prior intent: %s", exc)

        if text is None and prior is None:
            return payload, "none", None

        resolved = resolve_slots(
            payload, text=text, prior=prior, today=self._today(), provider=self._provider
        )
        payload.update(resolved.slots)
        return payload, resolved.source, resolved.notice

    def handled_task_types(self) -> frozenset[str]:
        return frozenset()  # handles all task_types routed to it by the orchestrator

    def handle(
        self,
        task: Task,
        decision: RoutingDecision,
        *,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        t0 = time.monotonic()
        payload: dict[str, Any] = dict(task.payload)
        payload, extraction_source, date_notice = self._resolve_payload(payload, session=session)
        intent = parse_intent(payload)

        # ── 1. CLARIFY ────────────────────────────────────────────────────────
        # A follow-up whose dates were rejected (e.g. a same-day return for a trip
        # with nights) keeps the prior dates and asks, instead of re-searching.
        question = missing_slot_question(intent) or date_notice
        if question:
            return self._handle_clarify(
                question, intent, decision, t0, context, session, extraction_source
            )

        # ── 2. ROLE-BOUND CHECK ───────────────────────────────────────────────
        if intent.traveler_id is not None and str(context.user_id) != intent.traveler_id:
            return self._handle_role_blocked(intent, decision, t0, context, session)

        # ── 3. PROFILE (optional; failures are non-fatal) ─────────────────────
        profile = None
        if context.user_id is not None:
            try:
                profile = ProfileTool().get_profile(
                    ProfileIntent(), context=context, session=session
                )
            except Exception as exc:
                logger.warning(
                    "TravelAgent: ProfileTool failed (%s); continuing without preferences.", exc
                )

        # ── 4+5. RETRIEVE + EVALUATE + RANK ──────────────────────────────────
        shortlist, tools_used = retrieve_and_rank(
            intent=intent, context=context, session=session, profile=profile
        )

        if not shortlist:
            return self._handle_no_options(
                intent, decision, t0, tools_used, context, session, extraction_source
            )

        # ── 6. REFLECT (deterministic critic — no LLM) ────────────────────────
        hyp = TravelShortlistHypothesizer(shortlist)
        critic = TravelPolicyHonestyCritic()
        reflection: ReflectionResult = reflect(hypothesizer=hyp, critic=critic)

        # Use the reflected (possibly corrected) shortlist
        final_shortlist: list[RankedOption] = (
            reflection.value  # type: ignore[assignment]
            if isinstance(reflection.value, list)
            else shortlist
        )

        # ── 7. OPTIONAL LLM NARRATIVE ─────────────────────────────────────────
        route, model_provider = self._extraction_route(extraction_source)
        degraded = False

        if intent.explain_with_llm and self._provider is not None:
            try:
                final_shortlist, degraded = enrich_with_narratives(
                    final_shortlist, provider=self._provider
                )
                if not degraded:
                    route = AiRoute.llm
                    model_provider = (
                        getattr(self._provider, "model", None) or type(self._provider).__name__
                    )
                else:
                    logger.warning(
                        "TravelAgent: LLM narrative partially degraded; "
                        "templated fallback used for one or more options (NFR-R1)."
                    )
            except ProviderError as exc:
                logger.warning(
                    "TravelAgent: LLM narrative ProviderError (%s); "
                    "using templated fallbacks for all options (NFR-R1).",
                    exc.code,
                )
                degraded = True

        # ── 8. PERSIST ON SELECTION ───────────────────────────────────────────
        # One planning conversation -> one draft trip (UAT-2 Issue 4): the
        # selection is added to the conversation's draft trip, or starts a new
        # one when that trip is no longer a draft / is for another route or
        # departure date (a return-date-only change updates it in place).
        trip_id: str | None = None
        trip_persist_error: str | None = None
        trip_change: dict[str, Any] | None = None
        trip_message: str | None = None
        trip_notice: str | None = None

        if intent.selected_option_ids:
            try:
                check_action(context, AgentAction.TRIP_CREATE)
                selection = apply_trip_selection(
                    intent=intent,
                    shortlist=final_shortlist,
                    selected_ids=intent.selected_option_ids,
                    reflection_outcome_value=reflection.outcome.value,
                    context=context,
                    session=session,
                )
                trip_id = str(selection.trip.id)
                chosen = [
                    o for o in final_shortlist if o.option_id in set(intent.selected_option_ids)
                ]
                trip_message, trip_notice = _trip_messages(selection, chosen)
                trip_change = _trip_change(selection)
            except GuardrailBlocked as exc:
                trip_persist_error = str(exc)
                logger.warning("TravelAgent: trip write blocked: %s", exc)
            except SelectionUnavailable as exc:
                trip_persist_error = str(exc)
                logger.info("TravelAgent: selection not in shortlist: %s", exc)

        # ── 9. CONVERSATION MESSAGE ───────────────────────────────────────────
        if intent.conversation_id:
            try:
                content = _build_message_content(
                    final_shortlist, trip_message or trip_persist_error
                )
                append_assistant_message(
                    conversation_id=intent.conversation_id,
                    content=content,
                    tool_calls_trace={"tools_used": tools_used, "intent": intent_slots(intent)},
                    citations=reflection.citations,
                    session=session,
                )
            except Exception as exc:
                logger.warning("TravelAgent: append_assistant_message failed: %s", exc)

        # ── 10. DECISION LOG ──────────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        record = DecisionRecord(
            agent=AiAgent.travel,
            task_type=decision.task_type,
            route=route,
            model_provider=model_provider,
            input_summary={
                "origin": intent.origin,
                "destination": intent.destination,
                "departure_date": intent.departure_date,
                "travelers": intent.travelers,
                "has_selected_ids": bool(intent.selected_option_ids),
                "explain_with_llm": intent.explain_with_llm,
                "extraction_source": extraction_source,
            },
            tools_used=tools_used,
            output_summary={
                "shortlist_count": len(final_shortlist),
                "in_policy_count": sum(1 for o in final_shortlist if o.in_policy),
                "reflection_outcome": reflection.outcome.value,
                "trip_id": trip_id,
                "trip_action": trip_change["action"] if trip_change else None,
                "degraded": degraded,
            },
            confidence=reflection.grounded.confidence,
            reflection_outcome=reflection.outcome,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        # ── RESULT ────────────────────────────────────────────────────────────
        status = AgentResultStatus.ok
        if not reflection.asserted or degraded or trip_persist_error:
            status = AgentResultStatus.needs_review

        output: dict[str, Any] = {
            "shortlist": [_option_to_dict(o, i + 1) for i, o in enumerate(final_shortlist)],
            "trip_id": trip_id,
            "clarify": False,
            "reflection_outcome": reflection.outcome.value,
            "degraded": degraded,
            "intent": intent_slots(intent),
        }
        if trip_change is not None:
            output["trip_change"] = trip_change
            output["trip_message"] = trip_message
            output["trip_notice"] = trip_notice
        if trip_persist_error:
            output["trip_persist_error"] = trip_persist_error

        return AgentResult(
            status=status,
            output=output,
            citations=reflection.citations,
            confidence=reflection.grounded.confidence,
            reflection_outcome=reflection.outcome,
        )

    # ── Private path helpers ──────────────────────────────────────────────────

    def _handle_clarify(
        self,
        question: str,
        intent: TripIntent,
        decision: RoutingDecision,
        t0: float,
        context: AgentContext,
        session: Session,
        extraction_source: str = "none",
    ) -> AgentResult:
        slots = intent_slots(intent)
        # Persist the question + the intent so far: the next turn's answer merges
        # onto it, and a reloaded conversation shows the question in its history.
        self._append_message(intent, question, {"intent": slots}, session)
        route, model_provider = self._extraction_route(extraction_source)
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.travel,
                task_type=decision.task_type,
                route=route,
                model_provider=model_provider,
                input_summary={
                    "origin": intent.origin,
                    "destination": intent.destination,
                    "departure_date": intent.departure_date,
                    "extraction_source": extraction_source,
                },
                tools_used={},
                output_summary={"clarify": True, "question_len": len(question)},
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            ),
        )
        return AgentResult(
            status=AgentResultStatus.ok,
            output={"clarify": True, "question": question, "intent": slots},
            citations=(),
            confidence=None,
            reflection_outcome=None,
        )

    def _extraction_route(self, extraction_source: str) -> tuple[AiRoute, str | None]:
        """route=llm (with the model id) only when the LLM actually extracted slots."""
        if extraction_source != "llm":
            return AiRoute.deterministic, None
        model = getattr(self._provider, "model", None) or type(self._provider).__name__
        return AiRoute.llm, str(model)

    @staticmethod
    def _append_message(
        intent: TripIntent, content: str, trace: dict[str, Any], session: Session
    ) -> None:
        if not intent.conversation_id:
            return
        try:
            append_assistant_message(
                conversation_id=intent.conversation_id,
                content=content,
                tool_calls_trace=trace,
                citations=(),
                session=session,
            )
        except Exception as exc:
            logger.warning("TravelAgent: append_assistant_message failed: %s", exc)

    def _handle_role_blocked(
        self,
        intent: TripIntent,
        decision: RoutingDecision,
        t0: float,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        reason = (
            "An employee may only plan trips for themselves (NFR-SEC6). "
            "The requested traveler_id does not match the authenticated user."
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.travel,
                task_type=decision.task_type,
                route=AiRoute.deterministic,
                input_summary={
                    "traveler_id": intent.traveler_id,
                    "user_id": str(context.user_id),
                },
                tools_used={},
                output_summary={"denied": True, "reason": reason},
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            ),
        )
        return AgentResult(
            status=AgentResultStatus.needs_review,
            output={"denied": True, "reason": reason},
            citations=(),
            confidence=None,
            reflection_outcome=None,
        )

    def _handle_no_options(
        self,
        intent: TripIntent,
        decision: RoutingDecision,
        t0: float,
        tools_used: dict[str, Any],
        context: AgentContext,
        session: Session,
        extraction_source: str = "none",
    ) -> AgentResult:
        slots = intent_slots(intent)
        message = _no_options_message(intent)
        self._append_message(intent, message, {"tools_used": tools_used, "intent": slots}, session)
        route, model_provider = self._extraction_route(extraction_source)
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.travel,
                task_type=decision.task_type,
                route=route,
                model_provider=model_provider,
                input_summary={
                    "origin": intent.origin,
                    "destination": intent.destination,
                    "departure_date": intent.departure_date,
                    "extraction_source": extraction_source,
                },
                tools_used=tools_used,
                output_summary={
                    "shortlist_count": 0,
                    "message": "No options found in inventory.",
                },
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            ),
        )
        return AgentResult(
            status=AgentResultStatus.ok,
            output={
                "shortlist": [],
                "message": message,
                "clarify": False,
                "intent": slots,
            },
            citations=(),
            confidence=None,
            reflection_outcome=None,
        )


def _no_options_message(intent: TripIntent) -> str:
    """Say what was searched, so an empty result is actionable rather than bare."""
    when = intent.departure_date or "the requested dates"
    if intent.return_date:
        when = f"{when} to {intent.return_date}"
    covered = ", ".join(f"{name} ({code})" for code, name in AIRPORT_CITIES.items())
    return (
        "No options found matching your request in the current inventory "
        f"({intent.origin} → {intent.destination}, {when}). "
        f"The demo inventory covers {covered}."
    )
