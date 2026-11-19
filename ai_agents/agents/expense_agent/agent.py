"""ExpenseAgent — deterministic-first expense report assembly agent (P4-T10, FR-X1..X7).

Implements the Agent protocol (P4-T7 contracts.py).
Registered as AiAgent.expense in the AgentRegistry by register_expense().

Pipeline (deterministic-first):
  1. Parse intent                      (FR-X1) — deterministic
  2. Role-bound check (own data only)  (NFR-SEC6) — deterministic
  3. Ingest receipts + card_txns + segments (FR-X1) — deterministic
  4. Smart-match (FR-X2)              — deterministic
  5. Categorize + itemize (FR-X3)     — deterministic
  6. Assemble DraftReport (FR-X4)     — deterministic
  7. Pre-submit check (FR-X5, M-A3)   — deterministic
  8. Reflect (ExpenseMatchHonestyCritic) (FR-A3) — deterministic
  9. Optional LLM narrative (FR-X6)   — llm (gated)
 10. Persist (flush-only)             (FR-X7) — deterministic
 11. Append ConversationMessage (if conv_id present)
 12. Write decision log (M-R4)        — always
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.assemble import DraftReport, assemble_report
from ai_agents.agents.expense_agent.categorize import build_draft_entries
from ai_agents.agents.expense_agent.ingest import IngestedData, ingest
from ai_agents.agents.expense_agent.intent import ExpenseIntent, parse_intent
from ai_agents.agents.expense_agent.matching import smart_match
from ai_agents.agents.expense_agent.narrative import enrich_with_narrative
from ai_agents.agents.expense_agent.persistence import (
    append_assistant_message,
    persist_expense_report,
)
from ai_agents.agents.expense_agent.presubmit import (
    PreSubmitFlag,
    build_flags_snapshot,
    run_presubmit,
)
from ai_agents.agents.expense_agent.reflection import (
    ExpenseMatchHonestyCritic,
    ExpenseMatchHypothesizer,
    ExpenseReflectionState,
)
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.exceptions import GuardrailBlocked
from ai_agents.guardrails.actions import AgentAction, check_action
from ai_agents.guardrails.reflection import ReflectionResult, reflect
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from database.models.ai_decision_log import AiAgent, AiRoute
from services.llm_provider.factory import get_llm_provider
from services.llm_provider.interface import LlmProvider

logger = logging.getLogger(__name__)


class ExpenseAgent:
    """Expense report assembly agent implementing the Agent protocol (P4-T7 contracts.py).

    Deterministic-first: the full ingest→match→categorize→assemble→presubmit→reflect
    pipeline runs without calling the LLM. The LLM is used ONLY for an optional
    narrative enrichment step, gated by payload["explain_with_llm"].
    """

    agent_type: AiAgent = AiAgent.expense

    def __init__(self, provider: LlmProvider | None = None) -> None:
        self._provider: LlmProvider = provider or get_llm_provider()

    def handled_task_types(self) -> frozenset[str]:
        return frozenset()

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
        intent = parse_intent(payload)

        # ── 1. ROLE-BOUND CHECK ───────────────────────────────────────────────
        if intent.owner_id is not None and str(context.user_id) != intent.owner_id:
            return self._handle_role_blocked(intent, decision, t0, context, session)

        # ── 2. INGEST ─────────────────────────────────────────────────────────
        data: IngestedData = ingest(intent, context=context, session=session)

        # ── 3. SMART-MATCH ────────────────────────────────────────────────────
        candidates = smart_match(data)

        # ── 4. CATEGORIZE + ITEMIZE ───────────────────────────────────────────
        entries = build_draft_entries(candidates)

        # Empty path (C7-23): nothing ACTIVE to assemble.  ``data.receipts`` / ``data.card_txns``
        # also carry already-linked receipts and already-matched transactions (kept only as
        # idempotency markers), so testing the RAW ingest lists let a message with no new
        # material persist an empty 0.00 draft report.  The draft entries are built from the
        # active (unlinked / unmatched) records only, so an empty entry list means there is
        # nothing to persist — never create a report for it.
        if not entries:
            return self._handle_empty(intent, decision, t0, data, context, session)

        # ── 5. ASSEMBLE ───────────────────────────────────────────────────────
        # trip_id already inferred during ingest
        report = assemble_report(
            entries,
            trip_id=data.trip_id,
            report_title=intent.report_title,
        )

        # ── 6. PRE-SUBMIT CHECK ────────────────────────────────────────────────
        flags, verdicts_index = run_presubmit(report, context=context, session=session)

        # ── 7. REFLECT (deterministic — no LLM) ──────────────────────────────
        state = ExpenseReflectionState(
            report=report,
            flags=flags,
            verdicts_index=verdicts_index,
        )
        hyp = ExpenseMatchHypothesizer(state)
        critic = ExpenseMatchHonestyCritic()
        reflection: ReflectionResult = reflect(hypothesizer=hyp, critic=critic)

        # Extract reflected state
        reflected_state = reflection.value
        if isinstance(reflected_state, ExpenseReflectionState):
            final_report = reflected_state.report
            final_flags = reflected_state.flags
        else:
            final_report = report
            final_flags = flags

        # ── 8. OPTIONAL LLM NARRATIVE ─────────────────────────────────────────
        route = AiRoute.deterministic
        model_provider: str | None = None
        degraded = False
        narrative: str | None = None

        if intent.explain_with_llm and self._provider is not None:
            try:
                narrative, degraded = enrich_with_narrative(
                    final_report, final_flags, provider=self._provider
                )
                if not degraded:
                    route = AiRoute.llm
                    model_provider = (
                        getattr(self._provider, "model", None) or type(self._provider).__name__
                    )
                else:
                    logger.warning(
                        "ExpenseAgent: LLM narrative degraded; templated fallback used (NFR-R1)."
                    )
            except Exception as exc:
                logger.warning(
                    "ExpenseAgent: LLM narrative failed (%s); "
                    "using templated fallback (NFR-R1).",
                    exc,
                )
                degraded = True

        # ── 9. PERSIST ────────────────────────────────────────────────────────
        report_id: str | None = None
        persist_error: str | None = None
        guardrail_blocked = False
        advisory_notes: list[str] = []

        try:
            check_action(context, AgentAction.EXPENSE_REPORT_CREATE)
            flags_snapshot = build_flags_snapshot(final_flags, len(final_report.entries))
            expense_report, advisory_notes = persist_expense_report(
                intent=intent,
                report=final_report,
                flags_snapshot=flags_snapshot,
                reflection_outcome_value=reflection.outcome.value,
                context=context,
                session=session,
            )
            report_id = str(expense_report.id)
            # UAT2 hardening (revision): persistence rebuilds the stored flags
            # once an itemized folio is shared with its siblings — the summary,
            # message and status below must describe what was STORED.
            stored = expense_report.pre_submit_flags
            if stored is not flags_snapshot and isinstance(stored, dict):
                final_flags = [
                    PreSubmitFlag.from_dict(fl)
                    for fl in (stored.get("flags") or [])
                    if isinstance(fl, dict)
                ]
        except GuardrailBlocked as exc:
            persist_error = str(exc)
            guardrail_blocked = True
            logger.warning("ExpenseAgent: EXPENSE_REPORT_CREATE blocked: %s", exc)

        # ── 10. CONVERSATION MESSAGE ───────────────────────────────────────────
        if intent.conversation_id:
            try:
                content = _build_message_content(final_report, final_flags, report_id)
                append_assistant_message(
                    conversation_id=intent.conversation_id,
                    content=content,
                    tool_calls_trace={"tools_used": {"PolicyEvalTool": True}},
                    citations=reflection.citations,
                    session=session,
                )
            except Exception as exc:
                logger.warning("ExpenseAgent: append_assistant_message failed: %s", exc)

        # ── 11. DECISION LOG ──────────────────────────────────────────────────
        latency_ms = int((time.monotonic() - t0) * 1000)
        blocking_count = sum(1 for f in final_flags if f.severity == "blocking")
        unmatched_count = sum(1 for c in candidates if c.receipt is None or c.card_txn is None)
        record = DecisionRecord(
            agent=AiAgent.expense,
            task_type=decision.task_type,
            route=route,
            model_provider=model_provider,
            input_summary={
                "trip_id": intent.trip_id,
                "explain_with_llm": intent.explain_with_llm,
                "receipt_count": len(data.receipts),
                "card_txn_count": len(data.card_txns),
                "segment_count": len(data.segments),
            },
            tools_used={"PolicyEvalTool": True, "ItineraryCalendarTool": True},
            output_summary={
                "entry_count": len(final_report.entries),
                "total_amount": final_report.total_amount,
                "flag_count": len(final_flags),
                "blocking_count": blocking_count,
                "reflection_outcome": reflection.outcome.value,
                "report_id": report_id,
                "degraded": degraded,
                "unmatched_count": unmatched_count,
            },
            confidence=reflection.grounded.confidence,
            reflection_outcome=reflection.outcome,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        # ── RESULT ────────────────────────────────────────────────────────────
        status = AgentResultStatus.ok
        if (
            not reflection.asserted
            or degraded
            or blocking_count > 0
            or final_report.mixed_currency
            or guardrail_blocked
        ):
            status = AgentResultStatus.needs_review

        output: dict[str, Any] = {
            "report_id": report_id,
            "entry_count": len(final_report.entries),
            "total_amount": final_report.total_amount,
            "currency": final_report.currency,
            "title": final_report.title,
            "flag_count": len(final_flags),
            "blocking_count": blocking_count,
            "reflection_outcome": reflection.outcome.value,
            "degraded": degraded,
            "unprocessed_receipts": data.unprocessed_receipts,
            "mixed_currency": final_report.mixed_currency,
        }
        if persist_error:
            output["persist_error"] = persist_error
        if advisory_notes:
            output["advisory_notes"] = advisory_notes
        if narrative:
            output["narrative"] = narrative

        return AgentResult(
            status=status,
            output=output,
            citations=reflection.citations,
            confidence=reflection.grounded.confidence,
            reflection_outcome=reflection.outcome,
        )

    # ── Private path helpers ──────────────────────────────────────────────────

    def _handle_role_blocked(
        self,
        intent: ExpenseIntent,
        decision: RoutingDecision,
        t0: float,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        reason = (
            "An employee may only assemble expense reports for themselves (NFR-SEC6). "
            "The requested owner_id does not match the authenticated user."
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.expense,
                task_type=decision.task_type,
                route=AiRoute.deterministic,
                input_summary={
                    "owner_id": intent.owner_id,
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

    def _handle_empty(
        self,
        intent: ExpenseIntent,
        decision: RoutingDecision,
        t0: float,
        data: IngestedData,
        context: AgentContext,
        session: Session,
    ) -> AgentResult:
        latency_ms = int((time.monotonic() - t0) * 1000)
        write_decision_log(
            session,
            context=context,
            record=DecisionRecord(
                agent=AiAgent.expense,
                task_type=decision.task_type,
                route=AiRoute.deterministic,
                input_summary={"trip_id": intent.trip_id},
                tools_used={"ItineraryCalendarTool": True},
                output_summary={
                    "entry_count": 0,
                    "total_amount": 0.0,
                    "flag_count": 0,
                    "blocking_count": 0,
                    "reflection_outcome": None,
                    "report_id": None,
                    "degraded": False,
                    "unmatched_count": 0,
                },
                confidence=None,
                reflection_outcome=None,
                latency_ms=latency_ms,
            ),
        )
        return AgentResult(
            status=AgentResultStatus.ok,
            output={
                "entry_count": 0,
                "total_amount": 0.0,
                "message": "nothing to assemble",
                "unprocessed_receipts": data.unprocessed_receipts,
            },
            citations=(),
            confidence=None,
            reflection_outcome=None,
        )


def _build_message_content(
    report: DraftReport,
    flags: list[PreSubmitFlag],
    report_id: str | None,
) -> str:
    blocking = sum(1 for f in flags if f.severity == "blocking")
    lines = [
        f"Assembled expense report '{report.title}' with "
        f"{len(report.entries)} entries, total {report.total_amount:.2f} {report.currency}."
    ]
    if blocking:
        lines.append(f"{blocking} blocking issue(s) require attention.")
    if report_id:
        lines.append(f"Report saved (ID: {report_id}, status: draft).")
    return "\n".join(lines)
