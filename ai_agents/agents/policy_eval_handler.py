"""PolicyEvalHandler — deterministic rule evaluation (Cap 1, P4-T8).

DeterministicHandler registered under task_type='policy_evaluate'.
LLM-free: composes PolicyEvalTool and writes one ai_decision_logs row.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.presubmit import apply_affidavit_fields, verdicts_to_flags
from ai_agents.agents.process_checks import load_process_rules
from ai_agents.context import AgentContext
from ai_agents.decision_log import DecisionRecord, write_decision_log
from ai_agents.guardrails.citations import Citation, CitationKind
from ai_agents.orchestrator.envelopes import AgentResult, AgentResultStatus, RoutingDecision, Task
from ai_agents.tools.policy_eval import EvalItem, PolicyEvalTool, RuleVerdict
from ai_agents.tools.spend_policy import restore_spend
from database.models.ai_decision_log import AiAgent, AiRoute

logger = logging.getLogger(__name__)


class PolicyEvalHandler:
    """DeterministicHandler for Cap 1: policy rule evaluation (LLM-free).

    Registered with AgentRegistry under task_type='policy_evaluate'.
    """

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

        # C7-11: the backend loads the SUBJECT under the caller's read scope and passes one
        # unit per entry in ``items`` ({item, entry_ref, category, has_receipt}, plus the
        # optional C7R keys entry_id / receipt_issue).  The legacy
        # single-item shape (EvalItem fields at the payload top level) is still accepted.
        raw_units = payload.get("items")
        units: list[dict[str, Any]]
        if isinstance(raw_units, list):
            units = [u for u in raw_units if isinstance(u, dict)]
        else:
            units = [
                {
                    "item": {
                        k: v for k, v in payload.items() if k in EvalItem.__dataclass_fields__
                    },
                    "entry_ref": payload.get("ref_id") or "item",
                    "category": None,
                    "has_receipt": True,  # unknown receipt state: never fabricate a flag
                }
            ]

        try:
            # Product gap 2: a unit's ``spend`` facts arrive as a plain dict (asdict).
            parsed = [(EvalItem(**restore_spend(dict(u.get("item") or {}))), u) for u in units]
        except (TypeError, ValueError) as exc:
            # C7-11/C7-12: log the detail, NEVER echo exception text to the caller.
            logger.warning("PolicyEvalHandler: invalid payload: %s", exc)
            return AgentResult(
                status=AgentResultStatus.needs_review,
                output={"error": "invalid_eval_item", "verdict": "needs_review", "flags": []},
            )

        tool = PolicyEvalTool()
        all_verdicts: list[RuleVerdict] = []
        flags: list[dict[str, Any]] = []
        items_out: list[dict[str, Any]] = []
        evaluated_rule_count = 0
        for item, unit in parsed:
            try:
                verdict = tool.evaluate(item, context=context, session=session)
            except (TypeError, ValueError, AttributeError) as exc:
                # Defence in depth (C7-11): a wrongly typed field that slipped past the caller's
                # validation is an invalid input, not a 500 — fixed code, detail logged only.
                logger.warning("PolicyEvalHandler: invalid eval item: %s", type(exc).__name__)
                return AgentResult(
                    status=AgentResultStatus.needs_review,
                    output={"error": "invalid_eval_item", "verdict": "needs_review", "flags": []},
                )
            evaluated_rule_count = max(evaluated_rule_count, verdict.evaluated_rule_count)
            all_verdicts.extend(verdict.verdicts)
            entry_ref = str(unit.get("entry_ref") or item.ref_id or item.item_type)
            raw_entry_id = unit.get("entry_id")
            entry_id = str(raw_entry_id) if raw_entry_id is not None else None
            raw_issue = unit.get("receipt_issue")
            unit_flags, _ = verdicts_to_flags(
                list(verdict.verdicts),
                entry_ref=entry_ref,
                has_receipt=bool(unit.get("has_receipt", True)),
                category=str(unit.get("category") or ""),
                # C7R: the same RECEIPT-REQ wording as approval time — an attached
                # receipt that does not back the claim is NOT "no receipt attached".
                receipt_issue=str(raw_issue) if raw_issue else None,
                # C7R: the entry UUID travels on every flag, so same-label entries
                # stay attributable whatever their readable entry_ref is.
                entry_id=entry_id,
            )
            # Product gap 2 stage 2 (EXP-04): the same missing-receipt affidavit outcome
            # as the pre-submit check / approval time (the caller sends the entry's inputs).
            affidavit = unit.get("affidavit")
            if isinstance(affidavit, dict):
                unit_flags = _with_affidavit(unit_flags, affidavit, context, session)
            flags.extend(f.to_dict() for f in unit_flags)
            # C7R: non-rule flags the caller derived from the entry's evidence (e.g. a
            # possible duplicate claim) — only well-formed flag dicts are accepted.
            extra = unit.get("extra_flags")
            if isinstance(extra, list):
                flags.extend(
                    dict(f)
                    for f in extra
                    if isinstance(f, dict) and f.get("flag_type") and f.get("severity")
                )
            items_out.append(
                {
                    "entry_ref": entry_ref,
                    "entry_id": entry_id,
                    "item_type": item.item_type,
                    "in_policy": verdict.in_policy,
                    "verdicts": [_verdict_dict(v) for v in verdict.verdicts],
                }
            )

        top_verdict = overall_verdict(flags)
        in_policy = all(i["in_policy"] for i in items_out)
        reason: str | None = None
        if not parsed:
            # Nothing was evaluated (e.g. a report with no entries and no what-if context):
            # never report a vacuous "in_policy" — the caller must not read it as compliant.
            top_verdict = "needs_review"
            in_policy = False
            reason = "no_items_to_evaluate"

        # Build citations from relied-on clause refs (de-duplicated by ref)
        seen_refs: set[str] = set()
        citation_list: list[Citation] = []
        for v in all_verdicts:
            if v.source_clause_ref is None or v.source_clause_ref in seen_refs:
                continue
            seen_refs.add(v.source_clause_ref)
            citation_list.append(
                Citation(
                    kind=CitationKind.clause_ref,
                    ref=v.source_clause_ref,
                    detail=v.reason,
                    source="policy_rules",
                )
            )
        citations = tuple(citation_list)

        latency_ms = int((time.monotonic() - t0) * 1000)
        record = DecisionRecord(
            agent=AiAgent.policy,
            task_type=decision.task_type,
            route=AiRoute.deterministic,
            model_provider=None,
            input_summary={
                # Single-item keys kept byte-compatible (eval-db M-A3 keys rows by ref_id).
                "item_type": parsed[0][0].item_type if len(parsed) == 1 else None,
                "ref_id": parsed[0][0].ref_id if len(parsed) == 1 else None,
                "ref_ids": [i.ref_id for i, _ in parsed],
                "item_count": len(parsed),
                "item_types": sorted({i.item_type for i, _ in parsed}),
                "subject_type": payload.get("subject_type"),
                "subject_id": payload.get("subject_id"),
            },
            tools_used={"PolicyEvalTool": {"evaluated_rule_count": evaluated_rule_count}},
            output_summary={
                "in_policy": in_policy,
                "verdict": top_verdict,
                "verdict_count": len(all_verdicts),
                "flag_count": len(flags),
            },
            confidence=1.0,
            reflection_outcome=None,
            latency_ms=latency_ms,
        )
        write_decision_log(session, context=context, record=record)

        return AgentResult(
            status=AgentResultStatus.ok,
            output={
                "verdict": top_verdict,
                "flags": flags,
                "in_policy": in_policy,
                "items": items_out,
                "verdicts": [_verdict_dict(v) for v in all_verdicts],
                "evaluated_rule_count": evaluated_rule_count,
                "item_count": len(parsed),
                "reason": reason,
            },
            citations=citations,
            confidence=1.0,
            reflection_outcome=None,
        )


def _verdict_dict(v: RuleVerdict) -> dict[str, Any]:
    return {
        "rule_id": v.rule_id,
        "rule_type": v.rule_type,
        "applies_to": v.applies_to,
        "source_clause_ref": v.source_clause_ref,
        "in_policy": v.in_policy,
        "reason": v.reason,
        "requires_approver": v.requires_approver,
    }


def overall_verdict(flags: list[dict[str, Any]]) -> str:
    """Top-level verdict from the pre-submit flag mapping (C7-11).

    ``violation``    — any BLOCKING flag (policy_violation / missing_receipt);
    ``needs_review`` — only advisory flags (approval_required / not_evaluated / advisory);
    ``in_policy``    — no flags at all.
    """
    if any(f.get("severity") == "blocking" for f in flags):
        return "violation"
    if flags:
        return "needs_review"
    return "in_policy"


def _with_affidavit(
    flags: list[Any], fields: dict[str, Any], context: AgentContext, session: Session
) -> list[Any]:
    """EXP-04 on one unit's flags (``presubmit.apply_affidavit_fields``)."""
    try:
        owner = uuid.UUID(str(fields.get("owner_id")))
    except (TypeError, ValueError):
        return flags
    return apply_affidavit_fields(
        flags,
        process=load_process_rules(session, context.organization_id),
        context=context,
        session=session,
        owner_id=owner,
        receipt_linked=bool(fields.get("receipt_linked")),
        entry_id=fields.get("entry_id"),
        amount=fields.get("amount"),
        expense_date=fields.get("expense_date"),
        signed_at=fields.get("signed_at"),
        justification=fields.get("justification"),
        currency=fields.get("currency"),
    )
