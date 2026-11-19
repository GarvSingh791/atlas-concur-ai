"""The approver-GRADE level of an approval (follow-up, grades: a real SVP approval level).

Some flags can only be decided by an approver of a minimum grade: they name a ladder
grade as ``requires_approver`` — AIR-04 with no grade recorded, AIR-07 / LOD-05 /
NR-01 for a junior or unknown grade (SVP), the AIR-CABIN override (AIR-03 "with VP
approval") and a stale EXP-01 report (VP).  The approval's REQUIRED GRADE is the most
senior grade any of its flags names (``config.constants.grades.approver_grade``;
"manager" names a reporting line, not a grade, and requires none).

* Routing (``route_by_grade``, called after every escalation / re-queue by
  ``lifecycle.ensure_independent_approver``): walk the submitter's manager chain
  upwards to the first active manager / finance admin whose grade of record is at or
  above the required grade; else an active finance admin of sufficient grade; else a
  finance admin as a fallback with an explicit "no approver of the required grade"
  rationale.  Never the submitter.  A changed route is audited.
* Visibility (``approval_scope``): a manager also sees an approval routed to them by
  grade (an SVP further up the chain is not the submitter's direct manager).
* Enforcement (``ensure_approver_grade``, in ``service.decide_approval``): a decision
  by a user whose grade is below the required grade — or who has no grade — is refused
  with 403 ``INSUFFICIENT_APPROVER_GRADE``.  Segregation of duties is unchanged
  (checked first: nobody decides their own submission).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.errors import ForbiddenError
from backend.app.core.rbac import direct_report_ids, scope_query
from config.constants import grades as grade_ladder
from database.models.approval import Approval, ApprovalStatus
from database.models.identity import RoleKey, User, UserStatus

INSUFFICIENT_APPROVER_GRADE = "INSUFFICIENT_APPROVER_GRADE"

ROUTED_MANAGER_CHAIN = "manager_chain"
ROUTED_FINANCE_ADMIN = "finance_admin"
ROUTED_FALLBACK = "fallback_no_approver_of_grade"

_MAX_CHAIN = 25  # a cycle / runaway chain can never loop forever
_DECIDING_ROLES = (RoleKey.manager, RoleKey.finance_admin)


# ---------------------------------------------------------------------------
# Required grade
# ---------------------------------------------------------------------------


def required_grade(flags: Iterable[Any] | None) -> tuple[str | None, list[str]]:
    """(most senior grade the *flags* require, the clauses requiring it)."""
    needed: list[tuple[str, str | None]] = []
    for flag in flags or ():
        if not isinstance(flag, Mapping):
            continue
        code = grade_ladder.approver_grade(flag.get("requires_approver"))
        if code is not None:
            clause = flag.get("source_clause_ref")
            needed.append((code, clause if isinstance(clause, str) else None))
    top = grade_ladder.most_senior([c for c, _ in needed])
    if top is None:
        return None, []
    clauses = sorted({cl for c, cl in needed if c == top and cl})
    return top, clauses


def required_grade_of(approval: Approval) -> str | None:
    """The approval's required approver grade (from its server-written risk summary).

    The stored ``required_approver_grade`` and the flags are both read and the more
    senior one wins (fail closed if either is present).
    """
    summary = approval.risk_summary if isinstance(approval.risk_summary, Mapping) else {}
    from_flags, _ = required_grade(summary.get("flags") if isinstance(summary, Mapping) else None)
    stored = summary.get("required_approver_grade") if isinstance(summary, Mapping) else None
    return grade_ladder.most_senior([c for c in (from_flags, stored) if isinstance(c, str)])


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def _eligible(user: User | None, *, approval: Approval, need: str) -> bool:
    return (
        user is not None
        and user.organization_id == approval.organization_id
        and user.id != approval.submitter_id
        and user.status == UserStatus.active
        and user.role in _DECIDING_ROLES
        and grade_ladder.at_or_above(user.grade, need) is True
    )


def _chain_approver(session: Session, approval: Approval, need: str) -> uuid.UUID | None:
    submitter = session.get(User, approval.submitter_id)
    next_id = submitter.manager_id if submitter is not None else None
    seen: set[uuid.UUID] = set()
    while next_id is not None and next_id not in seen and len(seen) < _MAX_CHAIN:
        seen.add(next_id)
        user = session.get(User, next_id)
        if user is None or user.organization_id != approval.organization_id:
            return None
        if _eligible(user, approval=approval, need=need):
            return user.id
        next_id = user.manager_id
    return None


def _finance_approver(session: Session, approval: Approval, need: str) -> uuid.UUID | None:
    candidates = session.scalars(
        sa.select(User)
        .where(
            User.organization_id == approval.organization_id,
            User.role == RoleKey.finance_admin,
            User.status == UserStatus.active,
            User.id != approval.submitter_id,
            User.grade.isnot(None),
        )
        .order_by(User.created_at, User.id)
    )
    return next((u.id for u in candidates if _eligible(u, approval=approval, need=need)), None)


def _fallback_finance(session: Session, approval: Approval) -> uuid.UUID | None:
    return session.scalars(
        sa.select(User.id)
        .where(
            User.organization_id == approval.organization_id,
            User.role == RoleKey.finance_admin,
            User.status == UserStatus.active,
            User.id != approval.submitter_id,
        )
        .order_by(User.created_at, User.id)
    ).first()


def _who(need: str) -> str:
    rung = grade_ladder.grade(need)
    return f"{rung.title} ({rung.short})" if rung is not None and rung.short != rung.title else need


def route_by_grade(session: Session, approval: Approval) -> None:
    """Route a PENDING approval whose flags require an approver grade (flush-only).

    No-op when nothing requires a grade (the usual line-manager routing stands).
    """
    if approval.current_status != ApprovalStatus.pending:
        return
    summary = dict(approval.risk_summary or {})
    need, clauses = required_grade(summary.get("flags"))
    if need is None:
        return
    before = summary.get("routed_to_approver_id")
    target = _chain_approver(session, approval, need)
    how = ROUTED_MANAGER_CHAIN
    if target is None:
        target, how = _finance_approver(session, approval, need), ROUTED_FINANCE_ADMIN
    if target is None:
        target, how = _fallback_finance(session, approval), ROUTED_FALLBACK
    cited = ", ".join(clauses) if clauses else "policy"
    if how == ROUTED_FALLBACK:
        rationale = (
            f"No approver of grade {_who(need)} or above exists in the submitter's management "
            f"chain or among the finance admins ({cited}). Routed to a finance admin as a "
            f"fallback: only a user whose grade of record is {need} or above can decide it — "
            "record one on the Users page."
        )
    elif how == ROUTED_FINANCE_ADMIN:
        rationale = (
            f"{cited} requires an approver of grade {_who(need)} or above; nobody in the "
            "submitter's management chain holds it, so it is routed to a finance admin who does."
        )
    else:
        rationale = (
            f"{cited} requires an approver of grade {_who(need)} or above; routed to the first "
            "such approver in the submitter's management chain."
        )
    summary["required_approver_grade"] = need
    summary["grade_routing"] = {
        "required_grade": need,
        "clauses": clauses,
        "routed_by": how,
        "rationale": rationale,
    }
    summary["routed_to_approver_id"] = str(target) if target is not None else None
    if how == ROUTED_FALLBACK:
        summary["routing_fallback"] = "finance_admin"
    approval.risk_summary = summary  # reassign so the JSONB change is tracked
    session.flush()
    if summary["routed_to_approver_id"] != before:
        record_audit(
            session,
            organization_id=approval.organization_id,
            action="approval.routed_by_grade",
            entity_type="approval",
            entity_id=approval.id,
            actor_id=None,
            before_state={"routed_to_approver_id": before},
            after_state={
                "routed_to_approver_id": summary["routed_to_approver_id"],
                "required_approver_grade": need,
                "routed_by": how,
            },
        )


# ---------------------------------------------------------------------------
# Visibility + enforcement
# ---------------------------------------------------------------------------


def _routed_to(user: User) -> sa.ColumnElement[bool]:
    """Routed to *user* BY GRADE (``route_by_grade`` wrote ``grade_routing``).  A plain
    routed id never widens a manager's scope (notifications invariant)."""
    return sa.and_(
        Approval.risk_summary.has_key("grade_routing"),
        Approval.risk_summary["routed_to_approver_id"].astext == str(user.id),
    )


def approval_scope(stmt: sa.Select[Any], *, user: User, session: Session) -> sa.Select[Any]:
    """``scope_query`` for approvals, plus: a manager sees an approval routed to them by
    GRADE (an SVP above the submitter's direct manager).

    Both keys are written only by the server (``route_by_grade``), so this widens a
    manager's view by exactly the approvals the grade routing assigned them (tenant
    first).
    """
    if user.role != RoleKey.manager:
        return scope_query(
            stmt,
            user=user,
            ownership_column=Approval.submitter_id,
            org_column=Approval.organization_id,
            session=session,
        )
    visible = [user.id, *direct_report_ids(user, session)]
    return stmt.where(
        Approval.organization_id == user.organization_id,
        sa.or_(Approval.submitter_id.in_(visible), _routed_to(user)),
    )


def grade_requirement(session: Session, approval: Approval, viewer: User) -> dict[str, Any] | None:
    """What the approvals UI shows: the required grade, who it is routed to and whether
    *viewer* may decide.  None when the approval requires no grade."""
    need = required_grade_of(approval)
    if need is None:
        return None
    rung = grade_ladder.grade(need)
    summary = approval.risk_summary if isinstance(approval.risk_summary, Mapping) else {}
    routing = summary.get("grade_routing") if isinstance(summary, Mapping) else None
    routing = routing if isinstance(routing, Mapping) else {}
    routed_id = summary.get("routed_to_approver_id") if isinstance(summary, Mapping) else None
    routed_name: str | None = None
    try:
        rid = uuid.UUID(str(routed_id)) if routed_id else None
    except ValueError:
        rid = None
    if rid is not None:
        routed_name = session.scalar(
            sa.select(User.name).where(
                User.id == rid, User.organization_id == approval.organization_id
            )
        )
    _, clauses = required_grade(summary.get("flags") if isinstance(summary, Mapping) else None)
    can = grade_ladder.at_or_above(viewer.grade, need) is True
    viewer_title = grade_ladder.title_of(viewer.grade)
    if can:
        message = f"Requires an approver of grade {_who(need)} or above — your grade qualifies."
    else:
        yours = (
            f"your grade of record is {viewer_title}"
            if viewer_title
            else "you have no grade on record"
        )
        message = f"Only an approver of grade {_who(need)} or above can decide this; {yours}." + (
            f" It is routed to {routed_name}." if routed_name else ""
        )
    return {
        "required_grade": need,
        "required_title": rung.title if rung else need,
        "required_short": rung.short if rung else need,
        "clauses": clauses or list(routing.get("clauses") or []),
        "routed_to_approver_id": str(rid) if rid else None,
        "routed_to_name": routed_name,
        "routed_by": routing.get("routed_by"),
        "rationale": routing.get("rationale"),
        "viewer_can_decide": can,
        "message": message,
    }


def ensure_approver_grade(approval: Approval, user: User) -> None:
    """403 INSUFFICIENT_APPROVER_GRADE unless *user*'s grade meets the approval's
    required grade (no requirement -> no-op; no grade on record -> refused)."""
    need = required_grade_of(approval)
    if need is None:
        return
    if grade_ladder.at_or_above(user.grade, need) is True:
        return
    raise ForbiddenError(
        f"This approval requires an approver of grade {_who(need)} or above.",
        code=INSUFFICIENT_APPROVER_GRADE,
    )
