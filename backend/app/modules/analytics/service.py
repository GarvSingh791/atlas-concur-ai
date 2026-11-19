"""Analytics service — role-scoped dashboard overview.

Business logic only; no LLM calls.  All data is derived from existing ORM
models via rule-based aggregation.

Security:
  - Every query is tenant-scoped (organization_id filter applied unconditionally).
  - Employee and manager sections further narrow via explicit organization_id filters;
    manager queries also use direct_report_ids to scope to the manager's team.
  - Finance section uses explicit org filter on every aggregate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.rbac import direct_report_ids, scope_query
from backend.app.modules.analytics.schemas import (
    ActivityItem,
    AnalyticsOverviewResponse,
    ApprovalQueueItem,
    EmployeeOverview,
    FinanceOverview,
    KpiMetric,
    ManagerOverview,
    PolicyWarning,
    Recommendation,
)
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import RoleKey, User
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus

# Trips that consumed approved budget: approved, and completed (approved, then ended).
_BUDGET_POSTURE_TRIP_STATUSES = (TripStatus.approved, TripStatus.completed)

UNASSIGNED_DEPARTMENT = "Unassigned"


def _department_label(column: Any) -> sa.ColumnElement[str]:
    """``users.department`` trimmed; NULL / blank -> "Unassigned"."""
    return sa.func.coalesce(
        sa.func.nullif(sa.func.btrim(column), ""), sa.literal(UNASSIGNED_DEPARTMENT)
    )


# ---------------------------------------------------------------------------
# Employee overview
# ---------------------------------------------------------------------------


def _build_employee_overview(session: Session, user: User, now: datetime) -> EmployeeOverview:
    today = now.date()

    # --- KPIs ---

    # Upcoming Trips
    upcoming_trip_statuses = [
        TripStatus.draft,
        TripStatus.pending_approval,
        TripStatus.approved,
    ]
    upcoming_trips_count: int = (
        session.scalar(
            sa.select(sa.func.count(Trip.id)).where(
                Trip.organization_id == user.organization_id,
                Trip.traveler_id == user.id,
                Trip.status.in_(upcoming_trip_statuses),
                Trip.start_date >= today,
            )
        )
        or 0
    )

    # Pending Approvals (submitted by user)
    pending_approvals_count: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == user.organization_id,
                Approval.submitter_id == user.id,
                Approval.current_status == ApprovalStatus.pending,
            )
        )
        or 0
    )

    # Expense Status — submitted reports owned by user
    expense_status_count: int = (
        session.scalar(
            sa.select(sa.func.count(ExpenseReport.id)).where(
                ExpenseReport.organization_id == user.organization_id,
                ExpenseReport.owner_id == user.id,
                ExpenseReport.status == ExpenseReportStatus.submitted,
            )
        )
        or 0
    )

    # Receipt Processing — all receipts uploaded by user
    receipt_count: int = (
        session.scalar(
            sa.select(sa.func.count(Receipt.id)).where(
                Receipt.organization_id == user.organization_id,
                Receipt.uploaded_by == user.id,
            )
        )
        or 0
    )

    kpis = [
        KpiMetric(label="Upcoming Trips", value=float(upcoming_trips_count), unit="count"),
        KpiMetric(label="Pending Approvals", value=float(pending_approvals_count), unit="count"),
        KpiMetric(label="Expense Status", value=float(expense_status_count), unit="count"),
        KpiMetric(label="Receipt Processing", value=float(receipt_count), unit="count"),
    ]

    # --- AI Recommendations (rule-based, ≤3) ---
    recommendations: list[Recommendation] = []

    # Pending OCR receipts
    pending_receipts_count: int = (
        session.scalar(
            sa.select(sa.func.count(Receipt.id)).where(
                Receipt.organization_id == user.organization_id,
                Receipt.uploaded_by == user.id,
                Receipt.ocr_status == OcrStatus.pending,
            )
        )
        or 0
    )
    if pending_receipts_count > 0:
        recommendations.append(
            Recommendation(
                id="rec_pending_receipts",
                title="Receipts awaiting OCR",
                body=f"{pending_receipts_count} receipt(s) pending OCR processing.",
                type="info",
            )
        )

    # Soonest upcoming trip in next 7 days
    if len(recommendations) < 3:
        soonest_trip = session.scalars(
            sa.select(Trip)
            .where(
                Trip.organization_id == user.organization_id,
                Trip.traveler_id == user.id,
                Trip.status.in_(upcoming_trip_statuses),
                Trip.start_date >= today,
            )
            .order_by(Trip.start_date.asc())
            .limit(1)
        ).first()
        if soonest_trip is not None:
            days_until = (cast(date, soonest_trip.start_date) - today).days
            if days_until < 7:
                recommendations.append(
                    Recommendation(
                        id="rec_upcoming_trip",
                        title="Upcoming trip soon",
                        body=(
                            f"Trip to {soonest_trip.primary_destination} starts in "
                            f"{days_until} day(s)."
                        ),
                        type="action",
                    )
                )

    # Draft expense reports
    if len(recommendations) < 3:
        draft_reports_count: int = (
            session.scalar(
                sa.select(sa.func.count(ExpenseReport.id)).where(
                    ExpenseReport.organization_id == user.organization_id,
                    ExpenseReport.owner_id == user.id,
                    ExpenseReport.status == ExpenseReportStatus.draft,
                    # Product gap 4: a withdrawn report is closed, not awaiting submission.
                    ExpenseReport.withdrawn_at.is_(None),
                )
            )
            or 0
        )
        if draft_reports_count > 0:
            recommendations.append(
                Recommendation(
                    id="rec_draft_reports",
                    title="Draft expense reports",
                    body=f"{draft_reports_count} draft expense report(s) awaiting submission.",
                    type="action",
                )
            )

    # --- Policy warnings from pre_submit_flags ---
    policy_warnings: list[PolicyWarning] = []
    flagged_reports = session.scalars(
        sa.select(ExpenseReport).where(
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id == user.id,
            ExpenseReport.status == ExpenseReportStatus.submitted,
            ExpenseReport.pre_submit_flags.isnot(None),
        )
    ).all()

    for report in flagged_reports:
        if len(policy_warnings) >= 3:
            break
        flags = report.pre_submit_flags
        if not flags:
            continue
        # flags may be a list of dicts or a dict with a "warnings" key
        flag_list = flags if isinstance(flags, list) else flags.get("warnings", [])
        for flag in flag_list:
            if len(policy_warnings) >= 3:
                break
            if isinstance(flag, dict):
                policy_warnings.append(
                    PolicyWarning(
                        id=str(flag.get("id", f"warn_{len(policy_warnings)}")),
                        severity=flag.get("severity", "low"),
                        message=flag.get("message", str(flag)),
                        source_clause_ref=flag.get("source_clause_ref"),
                    )
                )

    return EmployeeOverview(
        kpis=kpis,
        ai_recommendations=recommendations[:3],
        policy_warnings=policy_warnings[:3],
    )


# ---------------------------------------------------------------------------
# Manager overview
# ---------------------------------------------------------------------------


def _build_manager_overview(session: Session, user: User, now: datetime) -> ManagerOverview:
    # --- Approval queue: pending approvals the manager can actually open ---
    # Same scope as GET /approvals for a manager (scope_query on submitter_id:
    # own + direct reports, org-scoped) — an org-wide queue listed rows that
    # 404 on open and leaked other teams' submissions (C7-14).
    pending_base = scope_query(
        sa.select(Approval).where(Approval.current_status == ApprovalStatus.pending),
        user=user,
        ownership_column=Approval.submitter_id,
        org_column=Approval.organization_id,
        session=session,
    )

    approval_queue_total: int = (
        session.scalar(sa.select(sa.func.count()).select_from(pending_base.subquery())) or 0
    )

    pending_approvals = session.scalars(
        pending_base.order_by(Approval.created_at.asc()).limit(20)
    ).all()

    # Bulk-load submitter names
    submitter_ids = {a.submitter_id for a in pending_approvals}
    submitter_map: dict = {}
    if submitter_ids:
        submitter_rows = session.scalars(sa.select(User).where(User.id.in_(submitter_ids))).all()
        submitter_map = {u.id: u.name for u in submitter_rows}

    amount_map = _subject_amounts(session, user, pending_approvals)

    approval_queue: list[ApprovalQueueItem] = []
    for appr in pending_approvals:
        age_days = (now - appr.created_at).days if appr.created_at else 0
        amount, currency = amount_map.get(appr.id, (None, None))
        approval_queue.append(
            ApprovalQueueItem(
                id=str(appr.id),
                subject_type=appr.subject_type.value,
                submitter_name=submitter_map.get(appr.submitter_id, "Unknown"),
                amount=amount,
                currency=currency,
                age_days=age_days,
                status=appr.current_status.value,
            )
        )

    # --- Team spend: expense reports visible to manager ---
    report_ids = direct_report_ids(user, session)
    visible_ids = [user.id, *report_ids]

    # Sum total_amount from expense reports scoped to manager's visible users
    total_spend_result = session.scalar(
        sa.select(sa.func.sum(ExpenseReport.total_amount)).where(
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id.in_(visible_ids),
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
        )
    )
    total_spend = float(str(total_spend_result)) if total_spend_result is not None else 0.0

    # Group expenses by category for manager's visible reports
    category_rows = session.execute(
        sa.select(Expense.category, sa.func.sum(Expense.amount).label("amount"))
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            ExpenseReport.organization_id == user.organization_id,
            ExpenseReport.owner_id.in_(visible_ids),
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
        )
        .group_by(Expense.category)
        .order_by(sa.func.sum(Expense.amount).desc())
    ).all()

    by_category = [
        {"category": row.category.value, "amount": float(row.amount)} for row in category_rows
    ]

    team_spend = {
        "total": total_spend,
        "currency": "USD",
        "by_category": by_category,
    }

    # --- Recent activity: events about the manager and their team's subjects ---
    recent_activity = _build_activity_items(session, user, visible_user_ids=visible_ids)

    return ManagerOverview(
        approval_queue=approval_queue,
        approval_queue_total=approval_queue_total,
        team_spend=team_spend,
        recent_activity=recent_activity,
    )


# ---------------------------------------------------------------------------
# Finance overview
# ---------------------------------------------------------------------------


def _build_finance_overview(session: Session, user: User, now: datetime) -> FinanceOverview:
    org_id = user.organization_id

    # --- KPIs ---

    # Consolidated Spend: SUM of approved/reimbursed expense reports, org-scoped
    consolidated_spend_result = session.scalar(
        sa.select(sa.func.sum(ExpenseReport.total_amount)).where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
        )
    )
    consolidated_spend = (
        float(str(consolidated_spend_result)) if consolidated_spend_result is not None else 0.0
    )

    # Automation Rate: count(auto_approved) / count(total) * 100
    total_approvals: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
            )
        )
        or 0
    )
    auto_approved_count: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
                Approval.current_status == ApprovalStatus.auto_approved,
            )
        )
        or 0
    )
    automation_rate = (auto_approved_count / total_approvals * 100) if total_approvals > 0 else 0.0

    # Exception Rate: count(rejected + changes_requested) / count(total) * 100
    exception_count: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
                Approval.current_status.in_(
                    [ApprovalStatus.rejected, ApprovalStatus.changes_requested]
                ),
            )
        )
        or 0
    )
    exception_rate = (exception_count / total_approvals * 100) if total_approvals > 0 else 0.0

    # Avg Cycle Time (PRD US-F3 AC2, product gap 1): submitted → REIMBURSED, in
    # days, over reimbursed reports only.  None (rendered "—") when nothing has
    # been reimbursed yet — never a misleading 0.
    reimbursement_cycle_result = session.scalar(
        sa.select(
            sa.func.avg(
                sa.func.extract(
                    "epoch",
                    ExpenseReport.reimbursed_at - ExpenseReport.submitted_at,
                )
                / 86400.0
            )
        ).where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.status == ExpenseReportStatus.reimbursed,
            ExpenseReport.reimbursed_at.isnot(None),
            ExpenseReport.submitted_at.isnot(None),
        )
    )
    avg_cycle_time: float | None = (
        round(float(reimbursement_cycle_result), 2)
        if reimbursement_cycle_result is not None
        else None
    )

    # Avg Time to Decision (the pre-gap-1 "cycle time"): avg of
    # (ApprovalStep.decided_at - ExpenseReport.submitted_at) in days.
    # Join ApprovalStep → Approval → ExpenseReport (expense_report subject type)
    cycle_time_result = session.scalar(
        sa.select(
            sa.func.avg(
                sa.func.extract(
                    "epoch",
                    ApprovalStep.decided_at - ExpenseReport.submitted_at,
                )
                / 86400.0
            )
        )
        .select_from(ApprovalStep)
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .join(
            ExpenseReport,
            sa.and_(
                Approval.subject_id == ExpenseReport.id,
                Approval.subject_type == ApprovalSubjectType.expense_report,
            ),
        )
        .where(
            Approval.organization_id == org_id,
            ApprovalStep.decided_at.isnot(None),
            ExpenseReport.submitted_at.isnot(None),
        )
    )
    avg_time_to_decision: float | None = (
        round(float(cycle_time_result), 2) if cycle_time_result is not None else None
    )

    # Budget Posture: SUM(budget_amount - estimated_cost) for approved trips, org-scoped.
    # Product-gaps integration: COMPLETED trips were approved too — the elapsed-trip
    # sweep (gap 8) moves past approved trips to completed, which must not make them
    # drop out of the posture.
    budget_posture_result = session.scalar(
        sa.select(sa.func.sum(Trip.budget_amount - Trip.estimated_cost)).where(
            Trip.organization_id == org_id,
            Trip.status.in_(_BUDGET_POSTURE_TRIP_STATUSES),
            Trip.budget_amount.isnot(None),
            Trip.estimated_cost.isnot(None),
        )
    )
    budget_posture = float(str(budget_posture_result)) if budget_posture_result is not None else 0.0

    kpis = [
        KpiMetric(label="Consolidated Spend", value=consolidated_spend, unit="currency"),
        KpiMetric(label="Automation Rate", value=round(automation_rate, 2), unit="percent"),
        KpiMetric(label="Exception Rate", value=round(exception_rate, 2), unit="percent"),
        KpiMetric(
            label="Avg Cycle Time",
            value=avg_cycle_time,
            unit="days",
            description="Average days from submission to (simulated) reimbursement.",
        ),
        KpiMetric(
            label="Avg Time to Decision",
            value=avg_time_to_decision,
            unit="days",
            description="Average days from submission to an approval decision.",
        ),
        KpiMetric(label="Budget Posture", value=budget_posture, unit="currency"),
    ]

    # --- Spend over time: GROUP BY month (YYYY-MM), approved/reimbursed, org-scoped ---
    spend_over_time_rows = session.execute(
        sa.select(
            sa.func.to_char(ExpenseReport.submitted_at, "YYYY-MM").label("period"),
            sa.func.sum(ExpenseReport.total_amount).label("amount"),
        )
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
            ExpenseReport.submitted_at.isnot(None),
        )
        .group_by(sa.func.to_char(ExpenseReport.submitted_at, "YYYY-MM"))
        .order_by(sa.text("period ASC"))
    ).all()

    spend_over_time = [
        {"period": row.period, "amount": float(row.amount)} for row in spend_over_time_rows
    ]

    # --- Spend by category: GROUP BY expense category, org-scoped ---
    spend_by_category_rows = session.execute(
        sa.select(
            Expense.category,
            sa.func.sum(Expense.amount).label("amount"),
        )
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
        )
        .group_by(Expense.category)
        .order_by(sa.func.sum(Expense.amount).desc())
    ).all()

    spend_by_category = [
        {"category": row.category.value, "amount": float(row.amount)}
        for row in spend_by_category_rows
    ]

    # --- Spend by department: GROUP BY the owner's users.department, org-scoped ---
    # QA round: was the e-mail local part (a stand-in from before the department
    # column existed).  NULL / blank departments group as "Unassigned".
    owner_dept = _department_label(User.department)
    spend_by_dept_rows = session.execute(
        sa.select(
            owner_dept.label("department"),
            sa.func.sum(ExpenseReport.total_amount).label("amount"),
        )
        .join(User, ExpenseReport.owner_id == User.id)
        .where(
            ExpenseReport.organization_id == org_id,
            ExpenseReport.status.in_(
                [ExpenseReportStatus.approved, ExpenseReportStatus.reimbursed]
            ),
        )
        .group_by(owner_dept)
        .order_by(sa.func.sum(ExpenseReport.total_amount).desc())
    ).all()

    # --- Per-department budget: SUM(Trip.budget_amount) by the traveler's department ---
    # Same department key as actual spend so the two series share a key space.
    dept_budget_rows = session.execute(
        sa.select(
            owner_dept.label("department"),
            sa.func.sum(Trip.budget_amount).label("budget"),
        )
        .join(User, Trip.traveler_id == User.id)
        .where(
            Trip.organization_id == org_id,
            Trip.budget_amount.isnot(None),
        )
        .group_by(owner_dept)
    ).all()
    dept_budget_map = {row.department: float(row.budget) for row in dept_budget_rows}

    # Keep `amount` (actual) for backward-compat with the dashboard; add `budget`
    # so the analytics screen can render budget-vs-actual grouped bars.
    spend_by_department = [
        {
            "department": row.department,
            "amount": float(row.amount),
            "budget": dept_budget_map.get(row.department, 0.0),
        }
        for row in spend_by_dept_rows
    ]
    # Departments with a budget but no approved spend yet (actual = 0).
    seen_depts = {row.department for row in spend_by_dept_rows}
    for dept, budget in dept_budget_map.items():
        if dept not in seen_depts:
            spend_by_department.append({"department": dept, "amount": 0.0, "budget": budget})

    # --- Policy compliance breakdown (org-scoped, rule-based — NO LLM) ---
    # Derived from approval outcomes, the same source as the automation/exception
    # KPIs: auto_approved = fully in policy; manual approve / changes_requested =
    # exception that needed human handling; rejected = out-of-policy violation.
    compliant_count: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
                Approval.current_status == ApprovalStatus.auto_approved,
            )
        )
        or 0
    )
    exception_breakdown: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
                Approval.current_status.in_(
                    [ApprovalStatus.approved, ApprovalStatus.changes_requested]
                ),
            )
        )
        or 0
    )
    violation_count: int = (
        session.scalar(
            sa.select(sa.func.count(Approval.id)).where(
                Approval.organization_id == org_id,
                Approval.current_status == ApprovalStatus.rejected,
            )
        )
        or 0
    )
    policy_compliance = {
        "compliant": compliant_count,
        "exceptions": exception_breakdown,
        "violations": violation_count,
    }

    # --- Recent activity: AuditLog, last 10, org-scoped ---
    recent_activity = _build_activity_items(session, user)

    return FinanceOverview(
        kpis=kpis,
        spend_over_time=spend_over_time,
        spend_by_category=spend_by_category,
        spend_by_department=spend_by_department,
        policy_compliance=policy_compliance,
        recent_activity=recent_activity,
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _subject_amounts(
    session: Session, user: User, approvals: Sequence[Approval]
) -> dict[uuid.UUID, tuple[float | None, str | None]]:
    """Map approval id → (amount, currency) from its subject (C7-14).

    Expense report → total_amount; trip → estimated_cost (same source as the
    GET /approvals list).  Subjects are loaded org-scoped and in bulk.
    """
    report_ids = [
        a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.expense_report
    ]
    trip_ids = [a.subject_id for a in approvals if a.subject_type == ApprovalSubjectType.trip]
    reports: dict[uuid.UUID, ExpenseReport] = {}
    trips: dict[uuid.UUID, Trip] = {}
    if report_ids:
        reports = {
            r.id: r
            for r in session.scalars(
                sa.select(ExpenseReport).where(
                    ExpenseReport.id.in_(report_ids),
                    ExpenseReport.organization_id == user.organization_id,
                )
            )
        }
    if trip_ids:
        trips = {
            t.id: t
            for t in session.scalars(
                sa.select(Trip).where(
                    Trip.id.in_(trip_ids), Trip.organization_id == user.organization_id
                )
            )
        }

    def _num(value: object) -> float | None:
        return float(str(value)) if value is not None else None

    out: dict[uuid.UUID, tuple[float | None, str | None]] = {}
    for a in approvals:
        if a.subject_type == ApprovalSubjectType.expense_report and a.subject_id in reports:
            r = reports[a.subject_id]
            out[a.id] = (_num(r.total_amount), r.currency)
        elif a.subject_type == ApprovalSubjectType.trip and a.subject_id in trips:
            t = trips[a.subject_id]
            out[a.id] = (_num(t.estimated_cost), t.currency)
    return out


def _team_activity_filter(user: User, visible_user_ids: Sequence[uuid.UUID]) -> sa.ColumnElement:
    """Audit rows a manager may see: their own actions, plus any event whose
    subject belongs to them or a direct report (C7-14).  Other teams' events and
    org-level entities (policy rules, jobs, …) acted on by others are excluded.
    """
    org_id = user.organization_id
    ids = list(visible_user_ids)

    def _owned(entity_type: str, id_col: Any, owner_col: Any, org_col: Any) -> sa.ColumnElement:
        return sa.and_(
            AuditLog.entity_type == entity_type,
            sa.exists(
                sa.select(sa.literal(1)).where(
                    id_col == AuditLog.entity_id, owner_col.in_(ids), org_col == org_id
                )
            ),
        )

    return sa.or_(
        AuditLog.actor_id == user.id,
        _owned(
            "expense_report",
            ExpenseReport.id,
            ExpenseReport.owner_id,
            ExpenseReport.organization_id,
        ),
        _owned("trip", Trip.id, Trip.traveler_id, Trip.organization_id),
        _owned("approval", Approval.id, Approval.submitter_id, Approval.organization_id),
        _owned("receipt", Receipt.id, Receipt.uploaded_by, Receipt.organization_id),
        sa.and_(
            AuditLog.entity_type == "expense",
            sa.exists(
                sa.select(sa.literal(1))
                .select_from(Expense)
                .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
                .where(
                    Expense.id == AuditLog.entity_id,
                    ExpenseReport.owner_id.in_(ids),
                    ExpenseReport.organization_id == org_id,
                )
            ),
        ),
    )


def activity_summary(action: str) -> str:
    """A readable sentence for an audit action key (QA round).

    ``expense_report.withdrawn`` -> "Expense report withdrawn".  Was
    "<action> on <entity_type> <uuid>": raw keys and an internal id on the dashboard.
    """
    subject, _, verb = action.partition(".")
    head = subject.replace("_", " ").strip().capitalize() or "Activity"
    tail = verb.replace("_", " ").replace(".", " ").strip().lower()
    return f"{head} {tail}" if tail else head


def _build_activity_items(
    session: Session,
    user: User,
    *,
    visible_user_ids: Sequence[uuid.UUID] | None = None,
) -> list[ActivityItem]:
    """Return the 10 most recent AuditLog entries visible to *user*.

    ``visible_user_ids=None`` → org-wide (finance).  A list → manager scope:
    events by the manager and events about those users' subjects.
    """
    stmt = sa.select(AuditLog).where(AuditLog.organization_id == user.organization_id)
    if visible_user_ids is not None:
        stmt = stmt.where(_team_activity_filter(user, visible_user_ids))
    logs = session.scalars(stmt.order_by(AuditLog.created_at.desc()).limit(10)).all()

    # Bulk-load actor names
    actor_ids = {log.actor_id for log in logs if log.actor_id is not None}
    actor_map: dict = {}
    if actor_ids:
        actor_rows = session.scalars(sa.select(User).where(User.id.in_(actor_ids))).all()
        actor_map = {u.id: u.name for u in actor_rows}

    items: list[ActivityItem] = []
    for log in logs:
        actor_name = actor_map.get(log.actor_id) if log.actor_id else None
        summary = activity_summary(log.action)
        items.append(
            ActivityItem(
                id=str(log.id),
                action=log.action,
                actor_name=actor_name,
                entity_type=log.entity_type,
                occurred_at=log.created_at,
                summary=summary,
            )
        )
    return items


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def get_overview(session: Session, current_user: User) -> AnalyticsOverviewResponse:
    """Return a role-scoped analytics overview for *current_user*."""
    now = datetime.now(timezone.utc)

    if current_user.role == RoleKey.employee:
        employee_data = _build_employee_overview(session, current_user, now)
        return AnalyticsOverviewResponse(
            role=current_user.role.value,
            generated_at=now,
            employee=employee_data,
            manager=None,
            finance=None,
        )

    elif current_user.role == RoleKey.manager:
        manager_data = _build_manager_overview(session, current_user, now)
        return AnalyticsOverviewResponse(
            role=current_user.role.value,
            generated_at=now,
            employee=None,
            manager=manager_data,
            finance=None,
        )

    elif current_user.role == RoleKey.finance_admin:
        finance_data = _build_finance_overview(session, current_user, now)
        return AnalyticsOverviewResponse(
            role=current_user.role.value,
            generated_at=now,
            employee=None,
            manager=None,
            finance=finance_data,
        )

    else:
        # Fail-safe: unrecognized role returns empty response
        from backend.app.core.errors import ForbiddenError

        raise ForbiddenError("Unrecognized role — access denied.")
