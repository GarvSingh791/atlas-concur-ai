"""Policy module router — policy Q&A, evaluation, and rule-authoring endpoints (P4-T13 Slice 2).

POST   /api/v1/policy/query
POST   /api/v1/policy/evaluate
POST   /api/v1/policies/{policy_id}/rules
GET    /api/v1/policy/rules/{rule_id}/history
"""

from __future__ import annotations

import dataclasses
import logging
import math
import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from ai_agents.agents.policy_agent import presentable_policy_output
from ai_agents.agents.policy_authoring import draft_rule_title
from ai_agents.agents.policy_rule_semantics import describe_rule, sanitize_reason
from ai_agents.orchestrator.envelopes import Task
from ai_agents.orchestrator.orchestrator import Orchestrator
from ai_agents.tools.policy_eval import EvalItem
from backend.app.core.audit import record_audit
from backend.app.core.database import get_db
from backend.app.core.errors import AppError, ErrorCode, UnprocessableEntityError
from backend.app.core.rbac import require_permission, require_role
from backend.app.modules.ai.runtime import build_agent_context, get_orchestrator
from backend.app.modules.policy import schemas
from backend.app.modules.policy.document import policy_document
from backend.app.modules.policy.history import rule_history
from backend.app.modules.policy.service import (
    CONTEXT_ENTRY_REF,
    attach_entry_ids,
    build_subject_eval_payloads,
    get_active_policy_for_org,
    get_policy_for_org,
    list_active_rules,
    update_rule_status,
)
from config.constants.permissions import POLICY_MANAGE, POLICY_READ
from database.models.identity import RoleKey, User
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
    PolicyRuleType,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["policy"])

# Whitelist: only EvalItem fields may be forwarded from the caller-supplied context.
# Prevents mass-assignment injection of arbitrary keys into the Task payload.
# AIR-04: the traveller's grade comes from the owner's user record only — a what-if
# can never claim one.
_ALLOWED_EVAL_CONTEXT_KEYS: frozenset[str] = frozenset(EvalItem.__dataclass_fields__) - {
    "traveller_grade"
}


# ---------------------------------------------------------------------------
# POST /policy/query
# ---------------------------------------------------------------------------


@router.post("/policy/query", response_model=schemas.PolicyQueryResponse)
def query_policy(
    body: schemas.PolicyQueryRequest,
    user: Annotated[User, Depends(require_permission(POLICY_READ))],
    db: Annotated[Session, Depends(get_db)],
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
) -> schemas.PolicyQueryResponse:
    """Ask a natural-language question grounded in policy documents."""
    task = Task(
        context_type="policy_qa",
        payload={"question": body.question},
        signals={},
        task_type=None,
    )
    agent_ctx = build_agent_context(user, db)
    outcome = orchestrator.handle_task(task, context=agent_ctx, session=db)
    db.commit()

    # C7-06: a withheld needs_review draft never leaks through answer_structured / citations.
    output, presented_citations = presentable_policy_output(
        outcome.result.output or {}, outcome.result.citations
    )
    citations = [c.to_dict() for c in presented_citations]

    answer_type = output.get("answer_type", "needs_review")
    structured = output.get("answer_structured")
    return schemas.PolicyQueryResponse(
        answer_type=answer_type,
        answer=output.get("answer"),
        citations=citations,
        answer_structured=structured if isinstance(structured, dict) else None,
        # C7-06 contract: explicit flag on needs_review answers only.
        needs_review=True if answer_type == "needs_review" else None,
    )


# ---------------------------------------------------------------------------
# POST /policy/evaluate
# ---------------------------------------------------------------------------


@router.post("/policy/evaluate", response_model=schemas.PolicyEvaluateResponse)
def evaluate_policy(
    body: schemas.PolicyEvaluateRequest,
    user: Annotated[User, Depends(require_permission(POLICY_READ))],
    db: Annotated[Session, Depends(get_db)],
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
) -> schemas.PolicyEvaluateResponse:
    """Evaluate a subject (expense report / entry) against the active policy rules.

    C7-11: the subject is LOADED under the caller's read scope (404 when missing or out of
    scope) and evaluated entry-by-entry with the pre-submit EvalItem builder; the response
    carries the top-level ``verdict`` (in_policy / violation / needs_review) and ``flags``.
    Errors are logged, never echoed.
    """
    units = build_subject_eval_payloads(
        db, user=user, subject_type=body.subject_type, subject_id=body.subject_id
    )
    context_fields = {
        k: v for k, v in (body.context or {}).items() if k in _ALLOWED_EVAL_CONTEXT_KEYS
    }
    if context_fields:  # keys that are not EvalItem fields are ignored (never forwarded)
        units.append(_context_unit(context_fields))

    task = Task(
        context_type="policy_qa",
        payload={
            "items": units,
            "subject_type": body.subject_type,
            "subject_id": str(body.subject_id),
        },
        signals={"policy_eval": True},
        task_type=None,
    )
    agent_ctx = build_agent_context(user, db)
    outcome = orchestrator.handle_task(task, context=agent_ctx, session=db)
    db.commit()

    output = dict(outcome.result.output) if outcome.result.output else {}
    if output.get("error"):
        # The handler already logged the detail; return the envelope's generic code only.
        raise UnprocessableEntityError("The evaluation input could not be processed.")

    verdict = output.get("verdict")
    if verdict not in _EVAL_VERDICTS:
        logger.warning("policy evaluate: handler returned no usable verdict (%r)", verdict)
        verdict = "needs_review"
    attach_entry_ids(output, units)  # entry_ref is a label; flags also carry the UUID
    flags = output.get("flags")
    return schemas.PolicyEvaluateResponse(
        verdict=verdict,
        flags=flags if isinstance(flags, list) else [],
        output=output,
    )


_EVAL_VERDICTS: frozenset[str] = frozenset({"in_policy", "violation", "needs_review"})
_EVAL_ITEM_TYPES: frozenset[str] = frozenset({"flight", "hotel", "rail", "car", "meals", "receipt"})
_EVAL_NUMERIC_FIELDS: tuple[str, ...] = (
    "amount_usd",
    "duration_hours",
    "nights",
    "headcount",
    "claimed_nights",
    "days",
    "claimed_days",
    "room_only_nightly_usd",
    "tip_usd",
    "parking_days",
    "travel_days",
)
# Issue 6: stay-length counts divide an amount, so 0 / negatives are rejected (422).
_EVAL_COUNT_FIELDS: tuple[str, ...] = (
    "claimed_nights",
    "days",
    "claimed_days",
    "parking_days",
    "travel_days",
)
_EVAL_ITEM_ADAPTER: TypeAdapter[EvalItem] = TypeAdapter(EvalItem)


def _context_unit(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate the optional what-if EvalItem fields into one eval unit (422 when invalid).

    EvalItem is a plain dataclass (no runtime type checks), so the fields are validated
    against its annotations with pydantic: a wrongly typed value (``amount_usd: "abc"``,
    ``city: 5``, an unknown ``region``) is a 422 here instead of a TypeError -> 500 inside
    PolicyEvalTool.  Booleans and non-finite numbers are rejected for numeric fields.
    """
    if fields.get("item_type") not in _EVAL_ITEM_TYPES:
        raise UnprocessableEntityError(
            "context.item_type must be one of: " + ", ".join(sorted(_EVAL_ITEM_TYPES)) + "."
        )
    if any(isinstance(fields.get(key), bool) for key in _EVAL_NUMERIC_FIELDS):
        raise UnprocessableEntityError("The evaluation context is invalid.")
    try:
        item = _EVAL_ITEM_ADAPTER.validate_python(fields)
    except ValidationError as exc:
        logger.info("policy evaluate: invalid context item: %s", exc.errors(include_input=False))
        raise UnprocessableEntityError("The evaluation context is invalid.") from exc
    for key in _EVAL_NUMERIC_FIELDS:  # after coercion: "inf" / "nan" strings become floats
        value = getattr(item, key)
        if isinstance(value, float) and not math.isfinite(value):
            raise UnprocessableEntityError("The evaluation context is invalid.")
    for key in _EVAL_COUNT_FIELDS:
        value = getattr(item, key)
        if value is not None and (not isinstance(value, int) or value < 1):
            raise UnprocessableEntityError("The evaluation context is invalid.")
    return {
        "item": dataclasses.asdict(item),
        "entry_ref": CONTEXT_ENTRY_REF,
        "category": None,
        "has_receipt": True,
    }


# ---------------------------------------------------------------------------
# POST /policies/{policy_id}/rules
# ---------------------------------------------------------------------------


@router.post(
    "/policies/{policy_id}/rules",
    response_model=schemas.PolicyRuleResponse,
    status_code=201,
)
def create_policy_rule(
    policy_id: uuid.UUID,
    body: schemas.PolicyRuleCreate,
    user: Annotated[User, Depends(require_permission(POLICY_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
) -> schemas.PolicyRuleResponse:
    """Author a new draft policy rule via the AI agent.

    C7-04: the rule intent (title + description) is passed to the agent under the ``intent``
    key it reads; the created rule is taken from the AgentResult (``rule_id``), never guessed
    from the table.  When the agent did not create a draft, the response is a 422 and NO
    ``policy_rule.drafted`` audit row is written.  Only a ``draft`` rule of THIS policy created
    by THIS request's actor can be returned, so an existing (e.g. archived) rule can never be
    offered for "Approve & activate" through this path.

    Issue 5: a refused draft returns a STRUCTURED error — RULE_NOT_EXPRESSIBLE /
    RULE_CRITIC_REJECTED / RULE_INVALID (422) or PROVIDER_UNAVAILABLE (503) — whose message
    carries the critic / validator reason, and writes a ``policy_rule.draft_failed`` audit
    row (entity: the policy) with the original intent (<= 500 chars).
    """
    # 1. Verify policy exists and is org-scoped
    policy = get_policy_for_org(db, policy_id=policy_id, organization_id=user.organization_id)

    # 2. Build task with authoring signals.  ``intent`` is the free-text rule description the
    #    agent translates; rule_type / applies_to are optional caller hints folded into it.
    intent = _authoring_intent(body)
    task = Task(
        context_type="policy_qa",
        payload={
            "intent": intent,
            "policy_id": str(policy.id),
            "title": body.title,
            "description": body.description,
            "rule_type": body.rule_type,
            "applies_to": body.applies_to,
            "parameters": body.parameters or {},
        },
        signals={"policy_authoring": True},
        task_type=None,
    )

    # 3. Build agent context and run orchestrator (flush-only inside)
    agent_ctx = build_agent_context(user, db)
    outcome = orchestrator.handle_task(task, context=agent_ctx, session=db)
    output = dict(outcome.result.output) if outcome.result.output else {}

    # 4. Resolve the rule the agent REPORTS it created — and only if it is a fresh draft of
    #    this policy authored by this actor (never the newest arbitrary row).
    rule = _created_draft(db, output=output, policy_id=policy.id, actor_id=user.id)
    log_id = str(outcome.log_id) if outcome.log_id else None
    if rule is None:
        # Issue 5: keep the AI decision log AND a ``policy_rule.draft_failed`` audit row
        # (committed before raising), then return the structured reason — never a generic
        # message that hides why the draft was refused.  No rule row is written.
        error = authoring_error(output)
        record_audit(
            db,
            organization_id=user.organization_id,
            action="policy_rule.draft_failed",
            entity_type="policy",
            entity_id=policy.id,
            actor_id=user.id,
            after_state={
                "policy_id": str(policy.id),
                "failure_kind": error.kind,
                "error_code": error.code,
                "reason": error.reason,
                "repair_attempted": output.get("repair_attempted") is True,
                "intent": intent[:_AUDIT_INTENT_MAX],
                "intent_len": len(intent),
                "provenance": "ai_generated",
                "ai_decision_log_id": log_id,
            },
        )
        db.commit()
        raise AppError(error.message, status_code=error.status_code, code=error.code)

    # 5. Audit the NEW draft with its provenance (actor + AI-generated, draft status).
    record_audit(
        db,
        organization_id=user.organization_id,
        action="policy_rule.drafted",
        entity_type="policy_rule",
        entity_id=rule.id,
        actor_id=user.id,
        after_state={
            "status": rule.status.value,
            "policy_id": str(rule.policy_id),
            "rule_type": rule.rule_type.value,
            "applies_to": rule.applies_to.value,
            "source_clause_ref": rule.source_clause_ref,
            "title": draft_rule_title(rule.rule_type, rule.applies_to, rule.definition),
            "intent": intent[:_AUDIT_INTENT_MAX],
            "intent_len": len(intent),
            "repair_attempted": output.get("repair_attempted") is True,
            "provenance": "ai_generated",
            "ai_decision_log_id": log_id,
        },
    )

    # 6. Commit
    db.commit()

    # 7. Echo the full structured draft (rule_type/applies_to/definition) so the UI renders
    #    the "AI-Generated Draft" card without a second fetch.  ``version`` comes from the
    #    parent policy (PolicyRule has no version column).  UAT2-G: echo the intent that
    #    produced this draft (identical to the drafted audit's ``intent``).
    return schemas.PolicyRuleResponse.from_rule(
        rule, policy_version=policy.version, intent=intent[:_AUDIT_INTENT_MAX]
    )


# The original authoring intent kept in the audit trail (Issue 5 owner decision).
_AUDIT_INTENT_MAX = 500


@dataclasses.dataclass(frozen=True)
class AuthoringError:
    kind: str
    status_code: int
    code: str
    message: str
    reason: str


# failure_kind (PolicyAgent output) -> (HTTP status, error code, message prefix).
_AUTHORING_ERRORS: dict[str, tuple[int, str, str]] = {
    "unsupported": (422, "RULE_NOT_EXPRESSIBLE", "Atlas can't express this rule yet: "),
    "unfaithful": (
        422,
        "RULE_CRITIC_REJECTED",
        "The AI's draft didn't match your description, so it was discarded: ",
    ),
    "invalid": (422, "RULE_INVALID", "The AI couldn't produce a valid rule definition: "),
    "unavailable": (
        503,
        "PROVIDER_UNAVAILABLE",
        "The policy assistant is unavailable right now. Please try again shortly.",
    ),
    "denied": (403, ErrorCode.FORBIDDEN, "You are not allowed to author policy rules."),
}
_GENERIC_AUTHORING_MESSAGE = (
    "The AI could not produce a valid draft rule from this description. "
    "Rephrase the rule and try again."
)


def authoring_error(output: dict[str, Any]) -> AuthoringError:
    """Map the agent's ``failure_kind`` + reason to the client-facing error (Issue 5).

    The reason (LLM- or validator-written, influenced by the user's own intent) is
    re-sanitized and length-bounded here; the frontend renders it as plain text.
    An output without a known kind keeps the legacy generic VALIDATION_ERROR.
    """
    kind = output.get("failure_kind")
    reason = sanitize_reason(output.get("reason"))
    spec = _AUTHORING_ERRORS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        return AuthoringError(
            kind=str(kind or "unknown"),
            status_code=422,
            code=ErrorCode.VALIDATION,
            message=_GENERIC_AUTHORING_MESSAGE,
            reason=reason,
        )
    status_code, code, prefix = spec
    if kind in ("unavailable", "denied"):
        message = prefix
    else:
        detail = reason or "no reason was given."
        message = f"{prefix}{detail}"
        if kind == "unfaithful":
            message += " Try naming the expense type, the limit and what should happen."
    return AuthoringError(
        kind=str(kind), status_code=status_code, code=code, message=message, reason=reason
    )


_RULE_TYPE_VALUES: frozenset[str] = frozenset(e.value for e in PolicyRuleType)
_APPLIES_TO_VALUES: frozenset[str] = frozenset(e.value for e in PolicyAppliesTo)


def _authoring_intent(body: schemas.PolicyRuleCreate) -> str:
    """The NL intent the agent translates: title + description (+ known-enum hints only)."""
    parts = [body.title.strip()]
    if body.description and body.description.strip():
        parts.append(body.description.strip())
    hints = [
        f"{label}: {value}"
        for label, value, allowed in (
            ("rule_type", body.rule_type, _RULE_TYPE_VALUES),
            ("applies_to", body.applies_to, _APPLIES_TO_VALUES),
        )
        if value and value in allowed  # only known enum values; never free text
    ]
    if hints:
        parts.append("Hints: " + "; ".join(hints))
    return "\n".join(parts)


def _created_draft(
    db: Session,
    *,
    output: dict[str, Any],
    policy_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> PolicyRule | None:
    """The draft the agent reports it created — only a fresh draft of this policy by this actor."""
    if output.get("draft_created") is not True:
        return None
    try:
        rule_id = uuid.UUID(str(output.get("rule_id")))
    except (TypeError, ValueError):
        logger.warning("policy authoring: agent reported a draft without a valid rule_id")
        return None
    return db.scalars(
        sa.select(PolicyRule).where(
            PolicyRule.id == rule_id,
            PolicyRule.policy_id == policy_id,
            PolicyRule.status == PolicyRuleStatus.draft,
            PolicyRule.created_by == actor_id,
        )
    ).first()


# ---------------------------------------------------------------------------
# GET /policies — the active policy document (P5-T9 read surface)
# ---------------------------------------------------------------------------


@router.get("/policies", response_model=schemas.PolicyResponse)
def get_active_policy(
    user: Annotated[User, Depends(require_permission(POLICY_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.PolicyResponse:
    """Return the organization's active policy document (org-scoped).

    Follow-up: ``document`` carries the current corpus policy in sections with
    clause codes (what the Policy page renders).
    """
    policy = get_active_policy_for_org(db, organization_id=user.organization_id)
    response = schemas.PolicyResponse.model_validate(policy)
    response.document = policy_document(db, user.organization_id)
    return response


# ---------------------------------------------------------------------------
# GET /policy-rules — the active policy's structured rules (P5-T9 read surface)
# ---------------------------------------------------------------------------


@router.get("/policy-rules", response_model=list[schemas.PolicyRuleRead])
def list_policy_rules(
    user: Annotated[User, Depends(require_permission(POLICY_READ))],
    db: Annotated[Session, Depends(get_db)],
    status: str | None = None,
    applies_to: str | None = None,
) -> list[schemas.PolicyRuleRead]:
    """List the active policy's rules (flat list; org-scoped via the policy)."""
    policy, rules = list_active_rules(
        db,
        organization_id=user.organization_id,
        status=status,
        applies_to=applies_to,
    )
    return [schemas.PolicyRuleRead.from_rule(rule, policy_version=policy.version) for rule in rules]


# ---------------------------------------------------------------------------
# PATCH /policy-rules/{rule_id} — transition a rule's lifecycle status
# ---------------------------------------------------------------------------


@router.patch("/policy-rules/{rule_id}", response_model=schemas.PolicyRuleRead)
def patch_policy_rule_status(
    rule_id: uuid.UUID,
    body: schemas.PolicyRuleStatusUpdate,
    user: Annotated[User, Depends(require_permission(POLICY_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.PolicyRuleRead:
    """Activate or archive a rule (draft->active / draft->archived / active->archived).

    The AI authoring path only ever writes ``draft``; activation happens ONLY
    through this explicit, audited, POLICY_MANAGE-gated call (FR-A4 guardrail).
    """
    policy, rule, previous = update_rule_status(
        db,
        rule_id=rule_id,
        organization_id=user.organization_id,
        new_status=body.status,
        actor_id=user.id,
    )

    action = "policy_rule.activated" if body.status == "active" else "policy_rule.archived"
    record_audit(
        db,
        organization_id=user.organization_id,
        action=action,
        entity_type="policy_rule",
        entity_id=rule.id,
        actor_id=user.id,
        before_state={"status": previous},
        after_state={
            "status": body.status,
            "rule_type": rule.rule_type.value,
            "applies_to": rule.applies_to.value,
            "title": draft_rule_title(rule.rule_type, rule.applies_to, rule.definition),
            # What the activator was shown (the confirmation dialog restates it).
            "summary": list(describe_rule(rule.rule_type, rule.applies_to, rule.definition)),
        },
    )
    db.commit()

    return schemas.PolicyRuleRead.from_rule(rule, policy_version=policy.version)


# ---------------------------------------------------------------------------
# GET /policy/rules/{rule_id}/history — per-rule change history (product gap 8)
# ---------------------------------------------------------------------------


@router.get("/policy/rules/{rule_id}/history", response_model=schemas.RuleHistoryResponse)
def get_policy_rule_history(
    rule_id: uuid.UUID,
    user: Annotated[User, Depends(require_role(RoleKey.finance_admin, RoleKey.manager))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.RuleHistoryResponse:
    """Timestamped, audited changes of one rule (finance admins and managers; org-scoped).

    Read from audit_log (rule edits are already audited); only rule fields are returned —
    who, when, and each changed field's value before -> after.  A rule outside the caller's
    organization is a 404.  Employees are refused (403, deny-by-default).
    """
    return rule_history(db, rule_id=rule_id, organization_id=user.organization_id)
