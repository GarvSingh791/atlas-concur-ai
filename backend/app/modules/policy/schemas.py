from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_agents.agents.policy_authoring import draft_rule_title
from ai_agents.agents.policy_rule_semantics import describe_rule
from backend.app.modules.policy.document import PolicyDocument


class PolicyQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


class PolicyQueryResponse(BaseModel):
    """Policy Q&A answer (C7-06 shared contract).

    ``answer_type="needs_review"``: ``answer`` carries the critic-supported, cited DRAFT text
    (``answer_structured.full_text``) when one exists, else null; ``needs_review`` is True.
    It is a provisional answer and must never be rendered as grounded.  Silent / grounded
    shapes are unchanged (``needs_review`` stays null).
    """

    answer_type: str  # 'silent' | 'grounded' | 'needs_review'
    answer: str | None
    citations: list[Any]
    # Per-sentence breakdown ({full_text, needs_review, sentences}) as on the SSE result frame.
    answer_structured: dict[str, Any] | None = None
    needs_review: bool | None = None


# C7-12: the subject kinds /policy/evaluate can load and evaluate.  Anything else is a 422
# from request validation (previously a free string that 200'd with exception text).
EvaluateSubjectType = Literal["expense_report", "expense_entry"]


class PolicyEvaluateRequest(BaseModel):
    subject_type: EvaluateSubjectType
    subject_id: uuid.UUID
    # Optional what-if item (EvalItem fields, e.g. {"item_type": "hotel", "amount_usd": 389,
    # "city": "New York"}) evaluated IN ADDITION to the subject's entries.
    context: dict[str, Any] | None = None


class PolicyEvaluateResponse(BaseModel):
    verdict: str  # 'in_policy' | 'violation' | 'needs_review'
    flags: list[Any]
    output: dict[str, Any]  # full output for flexibility


class PolicyRuleCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    rule_type: str | None = None  # maps to PolicyRuleType enum if needed
    applies_to: str | None = None  # maps to PolicyAppliesTo enum if needed
    parameters: dict[str, Any] | None = None


def _enum_value(value: Any) -> Any:
    """Unwrap a SQLAlchemy/Python enum to its ``.value`` if present."""
    return value.value if hasattr(value, "value") else value


class PolicyRuleResponse(BaseModel):
    """Authoring response — echoes the full structured draft so the UI can
    render the "AI-Generated Draft" card without a second fetch (P5-T9)."""

    id: uuid.UUID
    policy_id: uuid.UUID
    title: str  # readable, derived from the validated rule (C7-04 / draft_rule_title)
    status: str  # 'draft'
    rule_type: str
    applies_to: str
    definition: dict[str, Any]
    # Issue 5: what the rule engine will actually do with the rule (describe_rule).
    summary: list[str] = Field(default_factory=list)
    source_clause_ref: str | None = None
    version: int
    # UAT2-G: the authoring intent that produced THIS draft (the same text the
    # ``policy_rule.drafted`` audit stores), so the UI labels the draft with what was
    # actually asked — never with whatever the textarea holds later.  None when unknown.
    intent: str | None = None
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_rule(
        cls, rule: Any, *, policy_version: int, intent: str | None = None
    ) -> PolicyRuleResponse:
        """Build from a PolicyRule ORM row (which has rule_type, not title).

        ``version`` is sourced from the parent policy — PolicyRule has no
        version column of its own.
        """
        rule_type = _enum_value(getattr(rule, "rule_type", None))
        return cls(
            id=rule.id,
            policy_id=rule.policy_id,
            # C7-04: a readable title ("Hotel cap — Tier 2 cities $275/night") built
            # deterministically from the validated rule, never from free user text.
            title=draft_rule_title(rule_type, rule.applies_to, rule.definition),
            status=_enum_value(rule.status),
            rule_type=str(rule_type),
            applies_to=str(_enum_value(rule.applies_to)),
            definition=rule.definition or {},
            summary=list(describe_rule(rule_type, rule.applies_to, rule.definition)),
            source_clause_ref=rule.source_clause_ref,
            version=policy_version,
            intent=intent,
        )


class PolicyResponse(BaseModel):
    """The active policy document (P5-T9 read surface).

    Follow-up: ``document`` is the CURRENT corpus policy document (Helios T&E
    policy) in sections with clause codes — what the Policy page shows; None
    when the organisation has no indexed corpus policy (the page then falls
    back to ``document_text``).
    """

    id: uuid.UUID
    name: str
    version: int
    document_text: str | None = None
    effective_from: date
    is_active: bool
    document: PolicyDocument | None = None
    model_config = ConfigDict(from_attributes=True)


class PolicyRuleRead(BaseModel):
    """A structured rule as rendered by the policy navigator (P5-T9)."""

    id: uuid.UUID
    policy_id: uuid.UUID
    # Issue 5: readable title (draft_rule_title) + what the engine actually does
    # (describe_rule). Both are total, so a legacy / malformed definition never 500s.
    title: str
    summary: list[str] = Field(default_factory=list)
    rule_type: str
    applies_to: str
    definition: dict[str, Any]
    source_clause_ref: str | None = None
    status: str
    version: int  # sourced from the parent policy
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_rule(cls, rule: Any, *, policy_version: int) -> PolicyRuleRead:
        return cls(
            id=rule.id,
            policy_id=rule.policy_id,
            title=draft_rule_title(rule.rule_type, rule.applies_to, rule.definition),
            summary=list(describe_rule(rule.rule_type, rule.applies_to, rule.definition)),
            rule_type=str(_enum_value(rule.rule_type)),
            applies_to=str(_enum_value(rule.applies_to)),
            definition=rule.definition or {},
            source_clause_ref=rule.source_clause_ref,
            status=str(_enum_value(rule.status)),
            version=policy_version,
            created_at=rule.created_at,
            updated_at=rule.updated_at,
        )


class PolicyRuleStatusUpdate(BaseModel):
    """Transition a rule's lifecycle status (P5-T9 activation/archival).

    ``draft`` is never a PATCH target — drafts are only created by the AI
    authoring path, never reverted to via this endpoint.
    """

    status: Literal["active", "archived"]


# ---------------------------------------------------------------------------
# Per-rule change history (product gap 8) — GET /policy/rules/{id}/history
# ---------------------------------------------------------------------------


class RuleFieldChange(BaseModel):
    """One changed field of a rule: ``definition.<key>`` for a definition key."""

    field: str
    before: Any = None
    after: Any = None


class RuleHistoryEntry(BaseModel):
    """One audited change of a rule (from audit_log; whitelisted fields only)."""

    id: uuid.UUID
    action: str
    label: str
    at: datetime
    actor_name: str | None = None  # None = system (migration / seed)
    changes: list[RuleFieldChange] = Field(default_factory=list)
    note: str | None = None


class RuleHistoryResponse(BaseModel):
    rule_id: uuid.UUID
    title: str
    created_at: datetime
    entries: list[RuleHistoryEntry] = Field(default_factory=list)
