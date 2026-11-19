"""Per-rule change history read from the audit log (product gap 8).

``rule_history`` returns the audited changes of ONE policy rule — who, when, and which
whitelisted field changed from what to what — for GET /policy/rules/{id}/history.

* Tenant scope: the rule must belong to a policy of the caller's organization
  (policy_rules has no org column — joined through policies); anything else is 404,
  indistinguishable from a missing rule.  audit_log rows are additionally filtered on
  ``organization_id`` (defence in depth).
* Only rule fields are exposed (status, title, type, target, clause, definition keys,
  and the system ``source`` note).  Free text the audit row may carry for other
  purposes (authoring intent, AI decision ids, activation summaries) is never returned.
* Read-only; never writes.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.policy_authoring import draft_rule_title
from backend.app.core.errors import NotFoundError
from backend.app.modules.policy.schemas import (
    RuleFieldChange,
    RuleHistoryEntry,
    RuleHistoryResponse,
)
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule

# Fields of an audit before/after state that describe the RULE (whitelist).
_RULE_FIELDS: tuple[str, ...] = (
    "status",
    "title",
    "rule_type",
    "applies_to",
    "source_clause_ref",
    "definition",
)
_NOTE_MAX = 200
HISTORY_LIMIT = 500

ACTION_LABELS: dict[str, str] = {
    "policy_rule.drafted": "Drafted (AI rule authoring)",
    "policy_rule.activated": "Activated",
    "policy_rule.archived": "Archived",
    "policy_rule.definition_updated": "Definition updated",
    "policy_rule.definition_reverted": "Definition reverted",
    "policy_rule.seeded": "Added to the policy",
    "policy_rule.unseeded": "Removed from the policy",
}


def _label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    tail = action.rsplit(".", 1)[-1].replace("_", " ").strip()
    return tail[:1].upper() + tail[1:] if tail else "Changed"


def field_changes(before: Any, after: Any) -> list[RuleFieldChange]:
    """Whitelisted field changes between two audit states (definition per top-level key).

    * creation (no rule field before — drafted / seeded): every field is new;
    * removal (no rule field after — unseeded): every field was removed;
    * update: only a field present on BOTH sides with different values.  A field only in
      ``after_state`` on an update (activation / archive rows restate title, rule_type,
      applies_to as context) did not change and is not reported (review fix).
    """
    b = before if isinstance(before, dict) else {}
    a = after if isinstance(after, dict) else {}
    created = not any(name in b for name in _RULE_FIELDS)
    removed = not created and not any(name in a for name in _RULE_FIELDS)
    changes: list[RuleFieldChange] = []
    for name in _RULE_FIELDS:
        if created:
            present = name in a
        elif removed:
            present = name in b
        else:
            present = name in b and name in a
        if not present:
            continue
        old, new = b.get(name), a.get(name)
        if name == "definition" and (isinstance(old, dict) or isinstance(new, dict)):
            old_d = old if isinstance(old, dict) else {}
            new_d = new if isinstance(new, dict) else {}
            for key in sorted(set(old_d) | set(new_d)):
                if old_d.get(key) != new_d.get(key):
                    changes.append(
                        RuleFieldChange(
                            field=f"definition.{key}", before=old_d.get(key), after=new_d.get(key)
                        )
                    )
            continue
        if old == new:
            continue
        changes.append(RuleFieldChange(field=name, before=old, after=new))
    return changes


def _note(after: Any) -> str | None:
    source = after.get("source") if isinstance(after, dict) else None
    if not isinstance(source, str) or not source.strip():
        return None
    return " ".join(source.split())[:_NOTE_MAX]


def rule_history(
    session: Session, *, rule_id: uuid.UUID, organization_id: uuid.UUID
) -> RuleHistoryResponse:
    """The audited history of one rule of the caller's organization (oldest first)."""
    rule = session.scalars(
        sa.select(PolicyRule)
        .join(Policy, PolicyRule.policy_id == Policy.id)
        .where(PolicyRule.id == rule_id, Policy.organization_id == organization_id)
    ).first()
    if rule is None:
        raise NotFoundError("Policy rule not found.")

    logs = list(
        session.scalars(
            sa.select(AuditLog)
            .where(
                AuditLog.organization_id == organization_id,
                AuditLog.entity_type == "policy_rule",
                AuditLog.entity_id == rule.id,
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(HISTORY_LIMIT)
        ).all()
    )
    logs.reverse()
    actor_ids = {log.actor_id for log in logs if log.actor_id is not None}
    names: dict[uuid.UUID, str] = {}
    if actor_ids:
        names = {
            u.id: u.name
            for u in session.scalars(
                sa.select(User).where(
                    User.id.in_(actor_ids), User.organization_id == organization_id
                )
            )
        }
    entries = [
        RuleHistoryEntry(
            id=log.id,
            action=log.action,
            label=_label(log.action),
            at=log.created_at,
            actor_name=names.get(log.actor_id) if log.actor_id else None,
            changes=field_changes(log.before_state, log.after_state),
            note=_note(log.after_state),
        )
        for log in logs
    ]
    return RuleHistoryResponse(
        rule_id=rule.id,
        title=draft_rule_title(rule.rule_type, rule.applies_to, rule.definition),
        created_at=rule.created_at,
        entries=entries,
    )
