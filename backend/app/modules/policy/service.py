from __future__ import annotations

import dataclasses
import logging
import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    affidavit_fields,
    build_entry_eval_item,
    build_eval_item,
    draft_entries_for_expenses,
    entry_label,
    evidence_flags,
    receipt_support_issue,
)
from ai_agents.agents.policy_rule_semantics import validate_rule_definition
from ai_agents.context import AgentContext
from ai_agents.tools.fx_policy import with_item_currency
from backend.app.core.demo import guard_seeded_rule_archive
from backend.app.core.errors import AppError, NotFoundError, UnprocessableEntityError
from backend.app.core.rbac import scope_query
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.policy import Policy
from database.models.policy_rule import (
    PolicyAppliesTo,
    PolicyRule,
    PolicyRuleStatus,
)

logger = logging.getLogger(__name__)

# Allowed forward lifecycle transitions (never silently revert to draft).
_ALLOWED_TRANSITIONS: dict[PolicyRuleStatus, set[PolicyRuleStatus]] = {
    PolicyRuleStatus.draft: {PolicyRuleStatus.active, PolicyRuleStatus.archived},
    PolicyRuleStatus.active: {PolicyRuleStatus.archived},
    PolicyRuleStatus.archived: set(),
}


def get_policy_for_org(
    session: Session,
    *,
    policy_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> Policy:
    """Load a policy scoped to the organization; raise NotFoundError if missing."""
    policy = session.scalars(
        sa.select(Policy).where(
            Policy.id == policy_id,
            Policy.organization_id == organization_id,
        )
    ).first()
    if policy is None:
        raise NotFoundError("Policy not found.")
    return policy


def get_active_policy_for_org(
    session: Session,
    *,
    organization_id: uuid.UUID,
) -> Policy:
    """Load the org's active policy document; raise NotFoundError if none."""
    policy = session.scalars(
        sa.select(Policy).where(
            Policy.organization_id == organization_id,
            Policy.is_active.is_(True),
        )
    ).first()
    if policy is None:
        raise NotFoundError("No active policy found.")
    return policy


def list_active_rules(
    session: Session,
    *,
    organization_id: uuid.UUID,
    status: str | None = None,
    applies_to: str | None = None,
) -> tuple[Policy, list[PolicyRule]]:
    """Return the active policy plus its rules (org-scoped via the policy).

    Optional ``status`` / ``applies_to`` filters mirror the navigator's filters;
    invalid enum values raise UnprocessableEntityError.
    """
    policy = get_active_policy_for_org(session, organization_id=organization_id)

    stmt = sa.select(PolicyRule).where(PolicyRule.policy_id == policy.id)
    if status is not None:
        try:
            stmt = stmt.where(PolicyRule.status == PolicyRuleStatus(status))
        except ValueError as exc:  # noqa: PERF203 — narrow, single value
            raise UnprocessableEntityError("Invalid rule status filter.") from exc
    if applies_to is not None:
        try:
            stmt = stmt.where(PolicyRule.applies_to == PolicyAppliesTo(applies_to))
        except ValueError as exc:
            raise UnprocessableEntityError("Invalid applies_to filter.") from exc

    stmt = stmt.order_by(PolicyRule.applies_to, PolicyRule.created_at)
    rules = list(session.scalars(stmt).all())
    return policy, rules


def update_rule_status(
    session: Session,
    *,
    rule_id: uuid.UUID,
    organization_id: uuid.UUID,
    new_status: str,
    actor_id: uuid.UUID,
) -> tuple[Policy, PolicyRule, str]:
    """Transition a rule's status with tenant + transition guards.

    Returns ``(policy, rule, previous_status_value)``. Flush-only — the caller
    writes the audit row and commits in the same transaction.
    """
    target = PolicyRuleStatus(new_status)

    # Join to the policy to enforce org-scoping (PolicyRule has no org column).  The rule
    # row is LOCKED (Issue 5) so two concurrent activate / archive calls serialize and the
    # transition guard below always sees the committed status.
    row = session.execute(
        sa.select(PolicyRule, Policy)
        .join(Policy, PolicyRule.policy_id == Policy.id)
        .where(
            PolicyRule.id == rule_id,
            Policy.organization_id == organization_id,
        )
        .with_for_update(of=PolicyRule)
        # Re-read the locked row even if it is already in the identity map.
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        raise NotFoundError("Policy rule not found.")
    rule, policy = row

    if target not in _ALLOWED_TRANSITIONS[rule.status]:
        raise UnprocessableEntityError(
            f"Cannot transition rule from {rule.status.value} to {target.value}."
        )

    # Phase 11 (DEMO_MODE): a seeded demo rule is never archived (audited 403).
    guard_seeded_rule_archive(session, policy=policy, rule=rule, actor_id=actor_id, target=target)

    # Issue 5: a rule the engine would silently mis-enforce is never activated.
    if target == PolicyRuleStatus.active:
        problems = validate_rule_definition(rule.rule_type, rule.applies_to, rule.definition)
        if problems:
            raise AppError(
                f"This rule can't be activated: {' '.join(problems[:2])}",
                status_code=422,
                code="RULE_INVALID",
            )

    previous = rule.status.value
    rule.status = target
    rule.updated_by = actor_id
    session.flush()
    return policy, rule, previous


# ---------------------------------------------------------------------------
# POST /policy/evaluate — subject loading (C7-11 / C7-12)
# ---------------------------------------------------------------------------


# entry_ref of the router's optional what-if unit; never used for a real entry.
CONTEXT_ENTRY_REF = "context"


def _unique_entry_labels(drafts: list[DraftEntry]) -> list[str]:
    """Approver-friendly AND unique entry_ref per entry (C7R).

    The label is the vendor (or category + amount + date).  Two entries with the
    same vendor ("United Airlines" x2) would be indistinguishable in the flags, so a
    repeated label gets the entry's category / amount / date appended, and a label
    that is still repeated gets an ordinal ("#2").  Never an internal UUID.
    """
    base = [entry_label(d) for d in drafts]
    counts: dict[str, int] = {}
    for label in base:
        counts[label] = counts.get(label, 0) + 1
    labels: list[str] = []
    for label, draft in zip(base, drafts, strict=True):
        if counts[label] > 1 or label == CONTEXT_ENTRY_REF:
            detail = entry_label(dataclasses.replace(draft, vendor=None))
            label = f"{label} ({detail})" if label != detail else label
        labels.append(label)
    # Ordinals for labels that are still repeated.  A generated "#n" label must not
    # collide with a label some OTHER entry already has (a claimant can type the
    # vendor "Cafe (Meals 10.00 USD on 2026-06-10) #2"), so every label that is
    # already unique is reserved first and an ordinal skips any taken name.
    totals: dict[str, int] = {}
    for label in labels:
        totals[label] = totals.get(label, 0) + 1
    taken: set[str] = {label for label in labels if totals[label] == 1}
    next_ordinal: dict[str, int] = {}
    unique: list[str] = []
    for label in labels:
        if totals[label] == 1:
            unique.append(label)
            continue
        n = next_ordinal.get(label, 0) + 1
        while f"{label} #{n}" in taken:
            n += 1
        next_ordinal[label] = n
        candidate = f"{label} #{n}"
        taken.add(candidate)
        unique.append(candidate)
    return unique


def attach_entry_ids(output: dict, units: list[dict]) -> None:
    """Add each unit's ``entry_id`` to the handler's per-item rows and flags (C7R).

    ``entry_ref`` is a readable label, so API clients need the entry UUID to tell
    which entry a flag belongs to.  The handler stamps ``entry_id`` on every flag
    and item row itself (from the unit); this is the fallback for rows that lack
    one.  ``items`` is aligned with ``units`` (same order); flags without an id are
    matched by their (unique) ``entry_ref``.  An id the handler already set is never
    overwritten.  The what-if context unit has no entry_id.  Mutates *output* in place.
    """
    id_by_ref = {
        str(u.get("entry_ref")): u["entry_id"] for u in units if u.get("entry_id") is not None
    }
    items = output.get("items")
    if isinstance(items, list) and len(items) == len(units):
        for row, unit in zip(items, units, strict=True):
            if isinstance(row, dict) and row.get("entry_id") is None:
                row["entry_id"] = unit.get("entry_id")
    flags = output.get("flags")
    if isinstance(flags, list):
        for flag in flags:
            if isinstance(flag, dict) and flag.get("entry_id") is None:
                flag["entry_id"] = id_by_ref.get(str(flag.get("entry_ref")))


def _entry_eval_payload(
    entry: Expense,
    draft: DraftEntry,
    *,
    idx: int,
    label: str,
    context: AgentContext,
    session: Session,
    owner_id: uuid.UUID,
) -> dict:
    """One evaluation unit for an ORM expense entry, built with the SHARED builder (C7R).

    ``build_entry_eval_item`` is the exact entry -> EvalItem mapping the approval-time
    re-evaluation uses (``run_presubmit``): supplier / city / nights come from trusted
    evidence (deterministic receipt OCR, the owner's card transaction, a human-approved
    trip segment) and never from claimant-typed fields; an unverifiable supplier / city
    stays fail-closed with an honest reason.  RECEIPT-REQ uses the same
    ``receipt_support_issue`` check, so /policy/evaluate and approval-time agree.

    ``entry_ref`` is the approver-friendly label (vendor, or category + amount + date;
    made unique within the subject) shown in flag reasons; the entry UUID stays in
    ``item.ref_id`` / ``entry_id`` and the router copies ``entry_id`` onto every flag /
    item row (``attach_entry_ids``).
    """
    try:
        item = build_entry_eval_item(
            draft, idx=idx, context=context, session=session, owner_id=owner_id
        )
    except Exception:  # malformed receipt / segment data must not 500 the endpoint
        logger.warning("policy evaluate: evidence lookup failed for entry %s", entry.id)
        # No evidence -> supplier / city unverified -> fail-closed flags.
        item = build_eval_item(draft, segment=None, idx=idx)
    item = dataclasses.replace(item, ref_id=str(entry.id))
    item = with_item_currency(item, draft.currency)  # product gap 8: compare in policy currency
    receipt_issue = receipt_support_issue(
        draft, context=context, session=session, owner_id=owner_id
    )
    # C7R: the same duplicate-claim check as approval time (a non-rule flag, so the
    # handler appends it to this unit's rule-derived flags); UAT2: plus the
    # receipt-reuse and receipt-date checks (``evidence_flags``).
    extra_flags = [
        flag.to_dict()
        for flag in evidence_flags(
            draft, entry_ref=label, context=context, session=session, owner_id=owner_id
        )
    ]
    return {
        "item": dataclasses.asdict(item),
        "entry_ref": label,
        "entry_id": str(entry.id),
        "category": draft.category,
        "has_receipt": receipt_issue is None,
        "receipt_issue": receipt_issue,
        "extra_flags": extra_flags,
        # Product gap 2 stage 2: EXP-04 inputs (same outcome as approval time).
        "affidavit": affidavit_fields(draft, owner_id=owner_id),
    }


def build_subject_eval_payloads(
    session: Session,
    *,
    user: User,
    subject_type: str,
    subject_id: uuid.UUID,
) -> list[dict]:
    """Load the evaluation subject under the caller's READ scope and build its eval units.

    ``expense_report`` -> one unit per entry; ``expense_entry`` -> that entry.  The report is
    resolved through ``scope_query`` (tenant first, then role: own / direct reports / org),
    so a subject outside the caller's scope is indistinguishable from a missing one (404).
    """
    if subject_type == "expense_report":
        report_stmt = scope_query(
            sa.select(ExpenseReport).where(ExpenseReport.id == subject_id),
            user=user,
            ownership_column=ExpenseReport.owner_id,
            org_column=ExpenseReport.organization_id,
            session=session,
        )
        report = session.scalars(report_stmt).first()
        if report is None:
            raise NotFoundError("Expense report not found.")
        entries = list(
            session.scalars(
                sa.select(Expense)
                .where(
                    Expense.report_id == report.id,
                    Expense.organization_id == user.organization_id,
                )
                .order_by(Expense.expense_date, Expense.id)
            ).all()
        )
    elif subject_type == "expense_entry":
        entry_stmt = scope_query(
            sa.select(Expense)
            .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
            .where(Expense.id == subject_id),
            user=user,
            ownership_column=ExpenseReport.owner_id,
            org_column=ExpenseReport.organization_id,
            session=session,
        )
        entry = session.scalars(entry_stmt).first()
        if entry is None:
            raise NotFoundError("Expense entry not found.")
        entries = [entry]
    else:  # pragma: no cover — the request schema restricts subject_type (422 earlier)
        raise UnprocessableEntityError("Unsupported subject_type.")

    if not entries:
        return []

    # Evidence (receipts / card txns / trip segments) is trusted only when it belongs to
    # the report OWNER — whoever is asking (owner, manager, finance).
    report_owner = session.scalar(
        sa.select(ExpenseReport.owner_id).where(ExpenseReport.id == entries[0].report_id)
    )
    if report_owner is None:  # pragma: no cover — FK guarantees the report exists
        raise NotFoundError("Expense report not found.")
    context = AgentContext(
        organization_id=user.organization_id,
        granted_permission_keys=frozenset(),
        user_id=user.id,
    )
    drafts = draft_entries_for_expenses(
        entries, session=session, organization_id=user.organization_id
    )
    labels = _unique_entry_labels(drafts)
    return [
        _entry_eval_payload(
            e,
            draft,
            idx=idx,
            label=label,
            context=context,
            session=session,
            owner_id=report_owner,
        )
        for idx, (e, draft, label) in enumerate(zip(entries, drafts, labels, strict=True))
    ]
