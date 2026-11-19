"""Global search (product gap 3 — the top-bar command search).

Searches ONLY what the caller may already read, through the same scope helpers
the list endpoints use:

* trips            — ``scope_query`` on ``Trip.traveler_id``          (trip.read.own)
* expense reports  — ``scope_query`` on ``ExpenseReport.owner_id``    (expense.read.own)
* expense entries  — joined to their parent report, which goes through the
                     report's ``scope_query`` (an entry is visible exactly when
                     its report is)                                   (expense.read.own)
* receipts         — ``scope_query`` on ``Receipt.uploaded_by``       (expense.read.own)
* approvals        — ``scope_query`` on ``Approval.submitter_id``     (approval.read only)
* policy rules     — the org's ACTIVE policy (policy.read); ``policy.manage``
                     holders also see draft / archived rules.

Matching is a case-insensitive substring (ILIKE) with LIKE wildcards escaped and
the term bound as a parameter (backend/app/core/text_search.py).  Every group is
hard-capped (``limit``, at most ``MAX_PER_GROUP``); one extra row is fetched to
report ``truncated``.  No new index: each query is already narrowed by the tenant
/ owner indexes, and a leading-wildcard ILIKE could not use a btree anyway (pg_trgm
is not installed and is deliberately not required).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ai_agents.agents.policy_authoring import draft_rule_title
from backend.app.core.errors import UnprocessableEntityError
from backend.app.core.rbac import get_user_permissions, scope_query
from backend.app.core.text_search import contains_ci
from backend.app.modules.notifications.service import approval_link, report_link, trip_link
from backend.app.modules.search.schemas import SearchGroup, SearchResponse, SearchResult
from config.constants.permissions import (
    APPROVAL_READ,
    EXPENSE_READ_OWN,
    POLICY_MANAGE,
    POLICY_READ,
    TRIP_READ_OWN,
)
from database.models.approval import Approval, ApprovalSubjectType
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport
from database.models.identity import User
from database.models.policy import Policy
from database.models.policy_rule import PolicyRule, PolicyRuleStatus
from database.models.receipt import Receipt
from database.models.trip import Trip

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 100
DEFAULT_PER_GROUP = 5
MAX_PER_GROUP = 10
# Policy rules are matched on their DERIVED title in Python; bound the rows read.
_MAX_RULES_SCANNED = 500


def normalise_query(raw: str | None) -> str:
    q = " ".join((raw or "").split())
    if len(q) < MIN_QUERY_LENGTH:
        raise UnprocessableEntityError(
            f"Search needs at least {MIN_QUERY_LENGTH} characters.", code="QUERY_TOO_SHORT"
        )
    if len(q) > MAX_QUERY_LENGTH:
        raise UnprocessableEntityError(
            f"Search is limited to {MAX_QUERY_LENGTH} characters.", code="QUERY_TOO_LONG"
        )
    return q


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PER_GROUP
    return max(1, min(MAX_PER_GROUP, limit))


def _money(amount: object, currency: str | None) -> str | None:
    if amount is None:
        return None
    return f"{amount} {currency}".strip() if currency else str(amount)


def _join(*parts: object) -> str | None:
    text = " · ".join(str(p) for p in parts if p not in (None, ""))
    return text or None


def _status(value: Any) -> str | None:
    return None if value is None else str(getattr(value, "value", value))


def _approval_links(
    session: Session, user: User, subject_type: ApprovalSubjectType, ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """subject id → approval link, only for approvals the caller may read."""
    if not ids:
        return {}
    stmt = scope_query(
        sa.select(Approval.subject_id, Approval.id).where(
            Approval.subject_type == subject_type, Approval.subject_id.in_(list(ids))
        ),
        user=user,
        ownership_column=Approval.submitter_id,
        org_column=Approval.organization_id,
        session=session,
    )
    return {sid: approval_link(aid) for sid, aid in session.execute(stmt).all()}


def _cap(rows: Sequence[Any], limit: int) -> tuple[list[Any], bool]:
    return list(rows[:limit]), len(rows) > limit


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


def _search_trips(
    session: Session, user: User, q: str, limit: int, *, can_approvals: bool
) -> SearchGroup:
    stmt = scope_query(
        sa.select(Trip).where(
            sa.or_(
                contains_ci(Trip.primary_destination, q),
                contains_ci(Trip.origin, q),
                contains_ci(Trip.purpose, q),
            )
        ),
        user=user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
        session=session,
    )
    rows, truncated = _cap(
        session.scalars(stmt.order_by(Trip.updated_at.desc(), Trip.id).limit(limit + 1)).all(),
        limit,
    )
    approvals = (
        _approval_links(session, user, ApprovalSubjectType.trip, [t.id for t in rows])
        if can_approvals
        else {}
    )
    items = [
        SearchResult(
            id=t.id,
            title=f"{t.origin} → {t.primary_destination}",
            subtitle=_join(f"{t.start_date} – {t.end_date}", t.purpose),
            status=_status(t.status),
            # My Trips lists only the caller's own trips; a teammate's trip is
            # opened through its approval.
            link=trip_link(t.id) if t.traveler_id == user.id else None,
            fallback_link=approvals.get(t.id),
        )
        for t in rows
    ]
    return SearchGroup(key="trips", label="Trips", items=items, truncated=truncated)


def _search_reports(
    session: Session, user: User, q: str, limit: int, *, can_approvals: bool
) -> SearchGroup:
    stmt = scope_query(
        sa.select(ExpenseReport).where(contains_ci(ExpenseReport.title, q)),
        user=user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    rows, truncated = _cap(
        session.scalars(
            stmt.order_by(ExpenseReport.updated_at.desc(), ExpenseReport.id).limit(limit + 1)
        ).all(),
        limit,
    )
    approvals = (
        _approval_links(session, user, ApprovalSubjectType.expense_report, [r.id for r in rows])
        if can_approvals
        else {}
    )
    items = [
        SearchResult(
            id=r.id,
            title=r.title,
            subtitle=_money(r.total_amount, r.currency),
            status=_status(r.status),
            link=report_link(r.id),
            fallback_link=approvals.get(r.id),
        )
        for r in rows
    ]
    return SearchGroup(key="reports", label="Expense reports", items=items, truncated=truncated)


def _search_entries(
    session: Session, user: User, q: str, limit: int, *, can_approvals: bool
) -> SearchGroup:
    stmt = scope_query(
        sa.select(Expense, ExpenseReport.title)
        .join(ExpenseReport, ExpenseReport.id == Expense.report_id)
        .where(
            Expense.organization_id == user.organization_id,
            sa.or_(contains_ci(Expense.vendor, q), contains_ci(Expense.description, q)),
        ),
        user=user,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    rows, truncated = _cap(
        session.execute(
            stmt.order_by(Expense.expense_date.desc(), Expense.id).limit(limit + 1)
        ).all(),
        limit,
    )
    approvals = (
        _approval_links(
            session,
            user,
            ApprovalSubjectType.expense_report,
            list({e.report_id for e, _t in rows}),
        )
        if can_approvals
        else {}
    )
    items = [
        SearchResult(
            id=e.id,
            title=e.vendor or e.description or "Expense",
            subtitle=_join(_money(e.amount, e.currency), e.expense_date, report_title),
            status=_status(e.status),
            link=report_link(e.report_id),
            fallback_link=approvals.get(e.report_id),
        )
        for e, report_title in rows
    ]
    return SearchGroup(key="entries", label="Expenses", items=items, truncated=truncated)


def _search_receipts(
    session: Session, user: User, q: str, limit: int, *, can_approvals: bool
) -> SearchGroup:
    vendor = Receipt.extracted_fields["vendor"].astext
    stmt = scope_query(
        sa.select(Receipt, Expense.report_id)
        .outerjoin(
            Expense,
            sa.and_(
                Expense.id == Receipt.expense_id,
                Expense.organization_id == Receipt.organization_id,
            ),
        )
        .where(contains_ci(vendor, q)),
        user=user,
        ownership_column=Receipt.uploaded_by,
        org_column=Receipt.organization_id,
        session=session,
    )
    rows, truncated = _cap(
        session.execute(
            stmt.order_by(Receipt.created_at.desc(), Receipt.id).limit(limit + 1)
        ).all(),
        limit,
    )
    report_ids = [rid for _r, rid in rows if rid is not None]
    approvals = (
        _approval_links(session, user, ApprovalSubjectType.expense_report, report_ids)
        if can_approvals
        else {}
    )
    items: list[SearchResult] = []
    for receipt, report_id in rows:
        fields = receipt.extracted_fields or {}
        if report_id is not None:
            link: str | None = report_link(report_id)
        elif receipt.uploaded_by == user.id:
            link = "/receipts"
        else:
            link = None
        items.append(
            SearchResult(
                id=receipt.id,
                title=str(fields.get("vendor") or "Receipt"),
                subtitle=_join(
                    _money(fields.get("amount"), fields.get("currency")), fields.get("date")
                ),
                status=_status(receipt.ocr_status),
                link=link,
                fallback_link=approvals.get(report_id) if report_id is not None else None,
            )
        )
    return SearchGroup(key="receipts", label="Receipts", items=items, truncated=truncated)


def _search_approvals(session: Session, user: User, q: str, limit: int) -> SearchGroup:
    report_match = (
        sa.select(Approval.id)
        .join(
            ExpenseReport,
            sa.and_(
                ExpenseReport.id == Approval.subject_id,
                ExpenseReport.organization_id == Approval.organization_id,
            ),
        )
        .where(
            Approval.subject_type == ApprovalSubjectType.expense_report,
            contains_ci(ExpenseReport.title, q),
        )
    )
    trip_match = (
        sa.select(Approval.id)
        .join(
            Trip,
            sa.and_(
                Trip.id == Approval.subject_id, Trip.organization_id == Approval.organization_id
            ),
        )
        .where(
            Approval.subject_type == ApprovalSubjectType.trip,
            sa.or_(
                contains_ci(Trip.primary_destination, q),
                contains_ci(Trip.origin, q),
                contains_ci(Trip.purpose, q),
            ),
        )
    )
    stmt = scope_query(
        sa.select(Approval).where(Approval.id.in_(sa.union(report_match, trip_match))),
        user=user,
        ownership_column=Approval.submitter_id,
        org_column=Approval.organization_id,
        session=session,
    )
    rows, truncated = _cap(
        session.scalars(
            stmt.order_by(Approval.updated_at.desc(), Approval.id).limit(limit + 1)
        ).all(),
        limit,
    )
    # Display titles + submitter names (batched, tenant-guarded).
    report_titles: dict[uuid.UUID, str] = {
        rid: title
        for rid, title in session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.title).where(
                ExpenseReport.organization_id == user.organization_id,
                ExpenseReport.id.in_(
                    [
                        a.subject_id
                        for a in rows
                        if a.subject_type == ApprovalSubjectType.expense_report
                    ]
                ),
            )
        )
    }
    trip_titles = {
        t.id: f"{t.origin} → {t.primary_destination}"
        for t in session.scalars(
            sa.select(Trip).where(
                Trip.organization_id == user.organization_id,
                Trip.id.in_(
                    [a.subject_id for a in rows if a.subject_type == ApprovalSubjectType.trip]
                ),
            )
        )
    }
    names: dict[uuid.UUID, str] = {
        uid: name
        for uid, name in session.execute(
            sa.select(User.id, User.name).where(
                User.organization_id == user.organization_id,
                User.id.in_({a.submitter_id for a in rows}),
            )
        )
    }
    items = [
        SearchResult(
            id=a.id,
            title=(
                trip_titles.get(a.subject_id)
                if a.subject_type == ApprovalSubjectType.trip
                else report_titles.get(a.subject_id)
            )
            or "Approval",
            subtitle=_join(
                names.get(a.submitter_id),
                "Trip" if a.subject_type == ApprovalSubjectType.trip else "Expense report",
            ),
            status=_status(a.current_status),
            link=approval_link(a.id),
        )
        for a in rows
    ]
    return SearchGroup(key="approvals", label="Approvals", items=items, truncated=truncated)


def _search_policy_rules(
    session: Session, user: User, q: str, limit: int, *, can_manage: bool
) -> SearchGroup:
    stmt = (
        sa.select(PolicyRule)
        .join(Policy, Policy.id == PolicyRule.policy_id)
        .where(Policy.organization_id == user.organization_id, Policy.is_active.is_(True))
    )
    if not can_manage:
        stmt = stmt.where(PolicyRule.status == PolicyRuleStatus.active)
    rules = session.scalars(
        stmt.order_by(PolicyRule.created_at, PolicyRule.id).limit(_MAX_RULES_SCANNED)
    ).all()
    needle = q.casefold()
    matched: list[tuple[PolicyRule, str]] = []
    for rule in rules:
        title = draft_rule_title(rule.rule_type, rule.applies_to, rule.definition)
        haystack = " ".join(
            (
                title,
                rule.source_clause_ref or "",
                _status(rule.rule_type) or "",
                _status(rule.applies_to) or "",
            )
        ).casefold()
        if needle in haystack:
            matched.append((rule, title))
    rows, truncated = _cap(matched, limit)
    items = [
        SearchResult(
            id=rule.id,
            title=title,
            subtitle=_join(rule.source_clause_ref, _status(rule.applies_to)),
            status=_status(rule.status),
            link="/policy",
        )
        for rule, title in rows
    ]
    return SearchGroup(key="policy_rules", label="Policy rules", items=items, truncated=truncated)


def search(
    session: Session, user: User, *, raw_query: str | None, limit: int | None
) -> SearchResponse:
    q = normalise_query(raw_query)
    per_group = clamp_limit(limit)
    perms = get_user_permissions(user, session)
    can_approvals = APPROVAL_READ in perms

    groups: list[SearchGroup] = []
    if TRIP_READ_OWN in perms:
        groups.append(_search_trips(session, user, q, per_group, can_approvals=can_approvals))
    if EXPENSE_READ_OWN in perms:
        groups.append(_search_reports(session, user, q, per_group, can_approvals=can_approvals))
        groups.append(_search_entries(session, user, q, per_group, can_approvals=can_approvals))
        groups.append(_search_receipts(session, user, q, per_group, can_approvals=can_approvals))
    if can_approvals:
        groups.append(_search_approvals(session, user, q, per_group))
    if POLICY_READ in perms:
        groups.append(
            _search_policy_rules(session, user, q, per_group, can_manage=POLICY_MANAGE in perms)
        )
    return SearchResponse(query=q, groups=[g for g in groups if g.items])
