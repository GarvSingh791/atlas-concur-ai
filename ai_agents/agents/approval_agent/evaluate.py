"""Subject loading and risk assessment for the Approval Agent (P4-T12).

load_subject  — resolve Trip or ExpenseReport from the DB, enforce org-scope.
assess_risk   — produce a RiskAssessment.  For expense reports it ALWAYS re-runs
                the deterministic pre-submit policy evaluation over the report's
                CURRENT entries (C7-05 — Decision B "never re-evaluate" is
                superseded) and merges the result with any stored flags.
build_risk_summary — serialise RiskAssessment to a JSONB-safe dict.

Policy signals consumed:
  Fresh evaluation of every Expense on the report through
    expense_agent.presubmit.run_presubmit(strict=True) → PolicyEvalTool
    (the same engine/item-builder as the assembly and receipt-upload paths).
  ExpenseReport.pre_submit_flags (built by P4-T10 pre-submit check / upload),
    merged in conservatively — a stored blocking flag can only ADD risk:
    { "flags": [...], "blocking_count": N, "advisory_count": N, ... }
    Each flag: { "flag_type": ..., "severity": "blocking"|"advisory",
                 "requires_approver": str|None, ... }

  is_policy_compliant is True ONLY when the fresh evaluation ran to completion
  over >= 1 USD entry and neither it nor the stored flags carry a blocking or
  policy_violation flag.  No entries, non-USD amounts, or an evaluation error
  → False (escalate to a human, with the reasons in policy_rationale).

  Trips: TripSegment.in_policy (bool|None, set by P4-T9 policy agent — but
  also accepted verbatim from POST /trips, i.e. claimant-asserted) is only a
  NECESSARY condition:
    None  → policy not yet evaluated → treat as out-of-policy (conservative)
    False → policy violation → out-of-policy
    True  → in policy, PROVIDED a fresh PolicyEvalTool run over the segments'
            declared values (same item builder as pre-submit) has no blocking
            flag, every amount is USD, and the threshold total is floored at the
            sum of the segment amounts (estimated_cost is client-supplied too).

manager_id for escalation routing is resolved once here from User.manager_id.

LAYERING LAW: never imports backend/.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, aliased

from ai_agents.agents import process_checks
from ai_agents.agents.expense_agent.assemble import DraftReport
from ai_agents.agents.expense_agent.categorize import DraftEntry
from ai_agents.agents.expense_agent.presubmit import (
    ESCALATING_FLAG_TYPES,
    build_eval_item,
    draft_entries_for_expenses,
    is_planner_booked_hotel,
    run_presubmit,
    verdicts_to_flags,
)
from ai_agents.agents.traveller_grade import owner_grade
from ai_agents.context import AgentContext
from ai_agents.receipt_pipeline.evidence import receipt_ids_by_entry
from ai_agents.receipt_pipeline.retention import SNAPSHOT_RECEIPTS_KEY
from ai_agents.tools.policy_eval import PolicyEvalTool
from config.constants.city_names import primary_city_key
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType
from database.models.approval_step import ApprovalDecision, ApprovalStep
from database.models.audit_log import AuditLog
from database.models.expense import Expense
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import User
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import TripSegment
from services.mock_inventory.locations import resolve_airport

logger = logging.getLogger(__name__)

# No FX conversion exists in the policy engine: amounts are compared as USD.
_POLICY_CURRENCY = "USD"


class SubjectLoadError(Exception):
    """Raised when the approval subject cannot be loaded or fails scope check."""


@dataclass(frozen=True)
class RiskAssessment:
    """Deterministic risk snapshot for one approval decision."""

    subject_type: ApprovalSubjectType
    subject_id: uuid.UUID
    submitter_id: uuid.UUID
    """owner_id (expense_report) or traveler_id (trip)."""
    manager_id: uuid.UUID | None
    """Resolved from User.manager_id; None if submitter has no manager."""
    subject_in_policy: bool
    """True iff ALL persisted policy signals indicate in-policy."""
    blocking_flag_count: int
    """Count of blocking flags: fresh + stored pre_submit_flags (expense) or the
    fresh segment evaluation (trip)."""
    requires_approver: bool
    """True if any flag carries a non-empty requires_approver value."""
    subject_total: float | None
    """Total amount in USD; None means unknown (triggers escalate)."""
    flags_snapshot: list[dict[str, Any]]
    """Raw flags list for serialisation into risk_summary."""
    is_policy_compliant: bool | None = None
    """Explicit compliance verdict from a FRESH policy evaluation (C7-05).

    Only ``True`` permits auto-approval; ``None`` (unknown) and ``False`` escalate.
    """
    policy_rationale: tuple[str, ...] = field(default_factory=tuple)
    """Human-readable reasons the subject is not policy-compliant (empty if it is)."""


def load_subject(
    subject_type: ApprovalSubjectType,
    subject_id: uuid.UUID,
    org_id: uuid.UUID,
    session: Session,
) -> Trip | ExpenseReport:
    """Load and org-scope a Trip or ExpenseReport.

    Raises:
        SubjectLoadError: Subject not found or belongs to a different org.
    """
    if subject_type == ApprovalSubjectType.trip:
        obj = session.get(Trip, subject_id)
        if obj is None:
            raise SubjectLoadError(f"Trip {subject_id} not found")
        if obj.organization_id != org_id:
            raise SubjectLoadError(f"Trip {subject_id} belongs to a different organisation")
        return obj

    if subject_type == ApprovalSubjectType.expense_report:
        report = session.get(ExpenseReport, subject_id)
        if report is None:
            raise SubjectLoadError(f"ExpenseReport {subject_id} not found")
        if report.organization_id != org_id:
            raise SubjectLoadError(
                f"ExpenseReport {subject_id} belongs to a different organisation"
            )
        return report

    raise SubjectLoadError(f"Unknown subject_type: {subject_type!r}")


def _resolve_manager(submitter_id: uuid.UUID, session: Session) -> uuid.UUID | None:
    user = session.get(User, submitter_id)
    if user is None:
        return None
    return user.manager_id


def _flag_key(flag: dict[str, Any]) -> tuple[Any, ...]:
    return (flag.get("entry_ref"), flag.get("rule_id"), flag.get("flag_type"))


def _is_blocking(flag: dict[str, Any]) -> bool:
    # A justified violation (C7R UAT) is advisory for SUBMIT only: it is still a
    # policy exception, so it keeps the report out of auto-approval.  UAT2: so is
    # an advisory receipt flag (unverifiable total, receipt dated far from the
    # claim) — ESCALATING_FLAG_TYPES always reach a human.
    return (
        flag.get("severity") == "blocking"
        or flag.get("flag_type") == "policy_violation"
        or flag.get("flag_type") in ESCALATING_FLAG_TYPES
        or bool(flag.get("justification"))
    )


def _fresh_policy_flags(
    report: ExpenseReport,
    *,
    context: AgentContext,
    session: Session,
) -> tuple[list[dict[str, Any]], list[str], float | None]:
    """Re-run the deterministic pre-submit evaluation over the report's entries.

    Returns (fresh_flags, reasons_not_evaluable, entries_total).  A non-empty
    reasons list means the report could NOT be shown to be compliant (→ escalate).
    """
    reasons: list[str] = []
    entries = list(
        session.scalars(
            sa.select(Expense)
            .where(
                Expense.report_id == report.id,
                Expense.organization_id == report.organization_id,
            )
            .order_by(Expense.created_at, Expense.id)
        ).all()
    )
    if not entries:
        reasons.append("Report has no expense entries to evaluate against policy.")
        return [], reasons, None

    currencies = {str(report.currency or "").upper()} | {
        str(e.currency or "").upper() for e in entries
    }
    if currencies != {_POLICY_CURRENCY}:
        reasons.append(
            "Report contains non-USD amounts "
            f"({', '.join(sorted(c or '?' for c in currencies))}); policy caps are in "
            "USD and no FX conversion is available, so compliance cannot be established."
        )

    draft_entries = draft_entries_for_expenses(
        entries, session=session, organization_id=report.organization_id
    )
    draft = DraftReport(
        entries=draft_entries,
        total_amount=float(str(report.total_amount or 0)),
        currency=report.currency,
        trip_id=str(report.trip_id) if report.trip_id else None,
        title=report.title,
    )
    try:
        flags, _ = run_presubmit(
            draft,
            context=context,
            session=session,
            owner_id=report.owner_id,
            strict=True,
        )
    except Exception as exc:
        logger.warning("ApprovalAgent: policy re-evaluation failed for %s: %s", report.id, exc)
        reasons.append("Policy evaluation could not be completed; human review required.")
        return [], reasons, None

    fresh: list[dict[str, Any]] = []
    for f in flags:
        d = f.to_dict()
        d["source"] = "approval_time_evaluation"
        fresh.append(d)
    entries_total = sum(float(str(e.amount)) for e in entries)
    return fresh, reasons, entries_total


def _assess_expense_report(
    report: ExpenseReport,
    *,
    context: AgentContext,
    session: Session,
) -> tuple[bool, int, bool, float | None, list, bool, tuple[str, ...]]:
    """Derive risk signals from a FRESH policy evaluation merged with stored flags.

    Returns:
        (subject_in_policy, blocking_flag_count, requires_approver, subject_total,
         flags_list, is_policy_compliant, policy_rationale)
    """
    fresh_flags, reasons, entries_total = _fresh_policy_flags(
        report, context=context, session=session
    )

    stored_snapshot: dict[str, Any] = report.pre_submit_flags or {}
    stored_flags: list[dict[str, Any]] = [
        f for f in (stored_snapshot.get("flags") or []) if isinstance(f, dict)
    ]
    try:
        stored_blocking = int(stored_snapshot.get("blocking_count", 0) or 0)
    except (TypeError, ValueError):
        stored_blocking = 1  # unreadable stored count → conservative

    # Merge: fresh flags first; stored flags not already re-derived are kept
    # (a stale stored flag can only ADD risk, never remove it).
    fresh_keys = {_flag_key(f) for f in fresh_flags}
    flags_list: list[dict[str, Any]] = list(fresh_flags) + [
        f for f in stored_flags if _flag_key(f) not in fresh_keys
    ]

    blocking_count = max(sum(1 for f in flags_list if _is_blocking(f)), stored_blocking)
    requires_approver = any(bool(f.get("requires_approver")) for f in flags_list)

    rationale = list(reasons)
    for f in flags_list:
        if _is_blocking(f):
            rationale.append(str(f.get("reason") or f.get("flag_type") or "blocking flag"))
    if blocking_count and not any(_is_blocking(f) for f in flags_list):
        rationale.append(f"Stored pre-submit check reported {blocking_count} blocking flag(s).")

    is_policy_compliant = not reasons and blocking_count == 0
    subject_in_policy = is_policy_compliant

    # Threshold amount: never below the sum of the entries actually evaluated
    # (defence in depth should a stored total ever lag its entries).
    total = report.total_amount
    subject_total: float | None = float(str(total)) if total is not None else None
    if subject_total is not None and entries_total is not None:
        subject_total = max(subject_total, entries_total)

    return (
        subject_in_policy,
        blocking_count,
        requires_approver,
        subject_total,
        flags_list,
        is_policy_compliant,
        tuple(rationale),
    )


# Trip segment type → expense category for the shared EvalItem builder.
_SEGMENT_CATEGORY = {
    "flight": "airfare",
    "hotel": "lodging",
    "car": "ground_transport",
    "rail": "other",
}


def _fresh_trip_flags(
    trip: Trip,
    segments: list[TripSegment],
    *,
    context: AgentContext,
    session: Session,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Evaluate the trip's declared segments with PolicyEvalTool (C7-05 review).

    Segment ``in_policy`` / ``estimated_cost`` arrive verbatim from POST /trips,
    so they cannot by themselves establish compliance.  Receipt rules do not
    apply to a plan (has_receipt=True suppresses missing_receipt).
    Returns (fresh_flags, reasons_not_evaluable).
    """
    reasons: list[str] = []
    currencies = {str(trip.currency or "").upper()} | {
        str(s.currency or trip.currency or "").upper() for s in segments
    }
    if currencies != {_POLICY_CURRENCY}:
        reasons.append(
            "Trip contains non-USD amounts "
            f"({', '.join(sorted(c or '?' for c in currencies))}); policy caps are in "
            "USD and no FX conversion is available, so compliance cannot be established."
        )
    entries: list[DraftEntry] = []
    items = []
    # AIR-04: the TRIP OWNER's grade of record (never the submitter's / a request's).
    grade = owner_grade(session, organization_id=trip.organization_id, user_id=trip.traveler_id)
    try:
        for idx, s in enumerate(segments):
            seg_type = getattr(s.segment_type, "value", str(s.segment_type))
            if s.amount is None:
                raise ValueError(f"segment {s.id} has no amount")
            entry = DraftEntry(
                category=_SEGMENT_CATEGORY.get(seg_type, "other"),
                amount=float(str(s.amount)),
                currency=s.currency or trip.currency,
                expense_date=str(s.start_datetime or trip.start_date),
                vendor=s.supplier,
                description=None,
                status="planned",
                match_source=None,
                trip_segment_id=str(s.id),
                is_itemized=False,
                primary_receipt_id=None,
                source_card_txn_id=None,
                sibling_group_id=None,
            )
            entries.append(entry)
            items.append(
                dataclasses.replace(
                    build_eval_item(entry, segment=s, idx=idx),
                    ref_id=f"segment_{s.id}",
                    # LOD-02 (review round 4): only a hotel the planner booked.
                    booked_through_tool=is_planner_booked_hotel(s, trip),
                    traveller_grade=grade,
                )
            )
        verdicts = PolicyEvalTool().evaluate_many(items, context=context, session=session)
        if len(verdicts) != len(items):
            raise ValueError(f"{len(verdicts)} verdicts for {len(items)} segments")
    except Exception as exc:
        logger.warning("ApprovalAgent: trip policy evaluation failed for %s: %s", trip.id, exc)
        reasons.append("Trip policy evaluation could not be completed; human review required.")
        return [], reasons

    fresh: list[dict[str, Any]] = []
    for entry, item, pv in zip(entries, items, verdicts, strict=True):
        seg_flags, _ = verdicts_to_flags(
            list(pv.verdicts),
            entry_ref=item.ref_id or "",
            has_receipt=True,
            category=entry.category,
        )
        for f in seg_flags:
            d = f.to_dict()
            d["source"] = "approval_time_evaluation"
            fresh.append(d)
    return fresh, reasons


def _assess_trip(
    trip: Trip,
    session: Session,
    *,
    context: AgentContext,
) -> tuple[bool, int, bool, float | None, list, bool, tuple[str, ...]]:
    """Derive risk signals for a trip: stored in_policy AND a fresh evaluation.

    Returns:
        (subject_in_policy, blocking_flag_count, requires_approver, subject_total,
         flags_list, is_policy_compliant, policy_rationale)
    """
    segments = list(
        session.scalars(sa.select(TripSegment).where(TripSegment.trip_id == trip.id)).all()
    )

    rationale: list[str] = []
    if not segments:
        rationale.append("Trip has no segments to evaluate against policy.")
    elif not all(s.in_policy is True for s in segments):
        rationale.append("One or more trip segments are not confirmed in policy.")

    fresh_flags: list[dict[str, Any]] = []
    if segments:
        fresh_flags, reasons = _fresh_trip_flags(trip, segments, context=context, session=session)
        rationale.extend(reasons)
        # Product gap 2 stage 2: PTA-02 / AIR-04 / AIR-06 / PTA-05 (fail closed).
        try:
            fresh_flags.extend(
                {**f, "source": "approval_time_evaluation"}
                for f in process_checks.trip_flags(trip, segments, session=session)
            )
        except Exception as exc:
            logger.warning("ApprovalAgent: trip process checks failed for %s: %s", trip.id, exc)
            rationale.append("Trip process checks could not be completed; human review required.")
    blocking_count = sum(1 for f in fresh_flags if _is_blocking(f))
    requires_approver = any(bool(f.get("requires_approver")) for f in fresh_flags)
    for f in fresh_flags:
        if _is_blocking(f):
            rationale.append(str(f.get("reason") or f.get("flag_type") or "blocking flag"))

    is_policy_compliant = not rationale
    subject_in_policy = is_policy_compliant

    # Prefer estimated_cost, then budget_amount; never below the sum of the
    # declared segment amounts (estimated_cost is client-supplied).
    cost = trip.estimated_cost
    if cost is None:
        cost = trip.budget_amount
    subject_total: float | None = float(str(cost)) if cost is not None else None
    if subject_total is not None:
        seg_sum = sum(float(str(s.amount)) for s in segments if s.amount is not None)
        subject_total = max(subject_total, seg_sum)

    # Minimal per-segment rows for risk_summary serialisation + fresh flags
    flags_list: list[dict[str, Any]] = [
        {
            "segment_id": str(s.id),
            "segment_type": s.segment_type.value,
            "in_policy": s.in_policy,
            "policy_notes": s.policy_notes,
        }
        for s in segments
    ] + fresh_flags

    return (
        subject_in_policy,
        blocking_count,
        requires_approver,
        subject_total,
        flags_list,
        is_policy_compliant,
        tuple(rationale),
    )


# C7R SoD: approval states in which a human reviewer has handed a subject back to its
# owner (mirrors backend lifecycle.REOPENABLE_ON_RESUBMISSION — duplicated here because
# the agent layer must never import backend/).
_RETURNED_APPROVAL_STATUSES = (ApprovalStatus.changes_requested, ApprovalStatus.rejected)
_HUMAN_RETURN_DECISIONS = (ApprovalDecision.reject, ApprovalDecision.request_changes)
OPEN_RETURNED_REPORT_FLAG = "open_returned_report"
REFILED_REJECTED_FLAG = "refiled_rejected_claim"
# Product-gaps integration (security review of gap 4): a line matching a report
# the owner WITHDREW after a reviewer's request-changes is never auto-approved —
# it goes to a human with this flag naming the original return.  It never blocks
# filing, and the fresh policy evaluation still runs.
REFILED_RETURNED_FLAG = "refiled_returned_claim"
# Reference kinds for the re-file check: content a reviewer REJECTED (or returned
# on a subject still open) vs content a reviewer RETURNED on a subject the owner
# then withdrew.
_REF_REJECTED = "rejected"
_REF_RETURNED = "returned"
# Re-file tolerance vs a REJECTED subject: a one-cent / one-day edit must not dodge
# review.  A match only routes to a human (never blocks the claimant), so err wide.
_REFILE_DATE_DAYS = 1
# UAT2 review: a report line may now be DELETED from a returned report (its frozen
# snapshot is the reference) and re-typed elsewhere.  Its date window matches the
# receipt-date tolerance (presubmit ``_RECEIPT_DATE_TOLERANCE_DAYS``): a line
# shifted 2-3 days must not slip between this check and the receipt-date check.
_REFILE_ENTRY_DATE_DAYS = 3
_REFILE_AMOUNT_ABS = Decimal("1.00")
_REFILE_AMOUNT_REL = Decimal("0.02")


def _open_returned_subject_flags(
    subject: Trip | ExpenseReport,
    subject_type: ApprovalSubjectType,
    session: Session,
) -> list[dict[str, Any]]:
    """Blocking flags for the owner's OTHER subjects a reviewer returned and has not resolved.

    A subject (expense report or trip) is an UNRESOLVED return while either
      * it is a DRAFT whose approval is changes_requested / rejected (the owner is
        mid-fix — rejected-then-reopened included), or
      * its approval is PENDING again with a human reject / request_changes step in its
        history (resubmitted after a return, awaiting the reviewer's re-decision).
    It stops counting once a reviewer decides it (approved, or rejected for good).  Case 2
    also requires the subject to still be awaiting that decision (report submitted / trip
    pending_approval) — an approval that can no longer be decided never penalises the owner
    (and cancelling a trip under review is refused, travel.service).  A
    finally rejected subject (never reopened) is a closed outcome and does not count —
    otherwise one old rejection would escalate every future submission; re-filing a
    rejected claim's items is caught by the duplicate-claim check instead (vendor-agnostic
    against rejected reports), and editing it requires reopening it (unresolved again).

    While the owner has such a subject, a NEW submission of the same type may be a re-filed
    copy of the returned items (retyped, lightly edited, vendor text changed) — content
    matching cannot reliably tell — so it must never auto-approve: it is routed to a human
    with a flag naming the returned subject.

    Product gap 4: a subject its owner WITHDREW (``withdrawn_at`` set — a report
    stays draft, a trip is cancelled) is closed and never counts, so one abandoned
    return no longer escalates every later submission forever.
    """
    if subject_type == ApprovalSubjectType.expense_report:
        model: Any = ExpenseReport
        draft: Any
        awaiting: Any = ExpenseReportStatus.submitted
        owner_col, label_col, draft = (
            ExpenseReport.owner_id,
            ExpenseReport.title,
            ExpenseReportStatus.draft,
        )
        owner_id = subject.owner_id  # type: ignore[union-attr]
        noun = "report"
    else:
        model = Trip
        awaiting = TripStatus.pending_approval
        owner_col, label_col, draft = Trip.traveler_id, Trip.primary_destination, TripStatus.draft
        owner_id = subject.traveler_id  # type: ignore[union-attr]
        noun = "trip"
    human_return_history = (
        sa.select(ApprovalStep.id)
        .where(
            ApprovalStep.approval_id == Approval.id,
            ApprovalStep.decision.in_(_HUMAN_RETURN_DECISIONS),
        )
        .exists()
    )
    rows = session.execute(
        sa.select(model.id, label_col)
        .join(
            Approval,
            sa.and_(
                Approval.subject_id == model.id,
                Approval.subject_type == subject_type,
                Approval.organization_id == model.organization_id,
            ),
        )
        .where(
            model.organization_id == subject.organization_id,
            owner_col == owner_id,
            model.id != subject.id,
            model.withdrawn_at.is_(None),
            sa.or_(
                sa.and_(
                    model.status == draft,
                    Approval.current_status.in_(_RETURNED_APPROVAL_STATUSES),
                ),
                sa.and_(
                    model.status == awaiting,
                    Approval.current_status == ApprovalStatus.pending,
                    human_return_history,
                ),
            ),
        )
        .order_by(label_col, model.id)
    ).all()
    return [
        {
            "flag_type": OPEN_RETURNED_REPORT_FLAG,
            "severity": "blocking",
            "requires_approver": None,
            "entry_ref": f"Returned {noun}",
            "related_subject_id": str(sid),
            "reason": (
                f"The submitter has a {noun} a reviewer returned that is not yet resolved "
                f"(\"{label or 'untitled'}\"). Check that this one does not re-file its items."
            ),
        }
        for sid, label in rows
    ]


LINES_REMOVED_FLAG = "lines_removed_since_return"


def _removed_since_return_flags(subject: ExpenseReport, session: Session) -> list[dict[str, Any]]:
    """Tell the reviewer how many lines the owner deleted since they returned the report.

    UAT2 (owner decision 5): a line may be deleted from a returned report (its
    content stays frozen on the return step's snapshot); the reviewer re-deciding
    the resubmission should see that lines are gone.  Counted from the
    append-only ``expense.deleted`` audit rows of this report written after its
    latest human return.  Advisory; the report goes back to a human anyway.
    """
    approval_ids = sa.select(Approval.id).where(
        Approval.organization_id == subject.organization_id,
        Approval.subject_type == ApprovalSubjectType.expense_report,
        Approval.subject_id == subject.id,
    )
    # The return's own audit row (same clock as the deletion rows); a legacy
    # return without one falls back to the step's decided_at.
    last_return = session.scalar(
        sa.select(sa.func.max(AuditLog.created_at)).where(
            AuditLog.organization_id == subject.organization_id,
            AuditLog.action == "approval.decided",
            AuditLog.entity_type == "approval",
            AuditLog.entity_id.in_(approval_ids),
            AuditLog.after_state["current_status"].astext.in_(
                [s.value for s in _RETURNED_APPROVAL_STATUSES]
            ),
        )
    ) or session.scalar(
        sa.select(sa.func.max(ApprovalStep.decided_at))
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .where(
            Approval.id.in_(approval_ids),
            ApprovalStep.decision.in_(_HUMAN_RETURN_DECISIONS),
        )
    )
    if last_return is None:
        return []
    removed = session.scalar(
        sa.select(sa.func.count(AuditLog.id)).where(
            AuditLog.organization_id == subject.organization_id,
            AuditLog.action == "expense.deleted",
            AuditLog.entity_type == "expense",
            AuditLog.before_state["report_id"].astext == str(subject.id),
            AuditLog.created_at >= last_return,
        )
    )
    if not removed:
        return []
    noun = "line" if removed == 1 else "lines"
    return [
        {
            "flag_type": LINES_REMOVED_FLAG,
            "severity": "advisory",
            "requires_approver": None,
            "entry_ref": "Report",
            "reason": (
                f"{removed} {noun} removed since this report was returned. The removed "
                f"{noun} stay on record in the audit trail."
            ),
        }
    ]


def _amount_close(a: Decimal, b: Decimal) -> bool:
    tol = max(_REFILE_AMOUNT_ABS, abs(b) * _REFILE_AMOUNT_REL)
    return abs(a - b) <= tol


def _reject_history(approval_id: Any) -> Any:
    """EXISTS: the approval has a human REJECT step (snapshot or not).

    Uses its own alias of approval_steps so it never auto-correlates to an outer
    query that already selects from approval_steps.
    """
    step = aliased(ApprovalStep)
    return (
        sa.select(step.id)
        .where(
            step.approval_id == approval_id,
            step.decision == ApprovalDecision.reject,
        )
        .exists()
    )


def _returned_snapshots(
    subject: Trip | ExpenseReport,
    subject_type: ApprovalSubjectType,
    owner_id: uuid.UUID,
    session: Session,
    *,
    include_withdrawn: bool = True,
) -> list[tuple[str, dict[str, Any], str, str]]:
    """(label, snapshot, kind, subject id) frozen on human return steps of the owner's
    OTHER subjects.

    ``kind`` is ``_REF_REJECTED`` for a REJECT snapshot, for a request-changes
    snapshot of a subject that is still open, and for any snapshot of a WITHDRAWN
    subject whose history holds a reject (a legacy pre-0014 reject froze nothing,
    so a later request-changes snapshot is the only record of rejected content).
    Product gap 4 / integration: a request-changes snapshot of a subject its owner
    WITHDREW (no reject in its history) is ``_REF_RETURNED`` — still a reference,
    so the abandoned return cannot be laundered into an auto-approval, but flagged
    as a re-filed RETURNED claim rather than a rejected one.

    Phase 10: ``include_withdrawn=False`` leaves WITHDRAWN subjects out — expense
    reports match those by lineage instead (``_withdrawn_report_refs``).
    """
    model: Any = ExpenseReport if subject_type == ApprovalSubjectType.expense_report else Trip
    label_col = (
        ExpenseReport.title
        if subject_type == ApprovalSubjectType.expense_report
        else Trip.primary_destination
    )
    rows = session.execute(
        sa.select(
            label_col,
            ApprovalStep.subject_snapshot,
            ApprovalStep.decision,
            model.withdrawn_at,
            _reject_history(Approval.id),
            model.id,
        )
        .select_from(ApprovalStep)
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .join(model, model.id == Approval.subject_id)
        .where(
            Approval.organization_id == subject.organization_id,
            Approval.subject_type == subject_type,
            Approval.submitter_id == owner_id,
            Approval.subject_id != subject.id,
            ApprovalStep.decision.in_(_HUMAN_RETURN_DECISIONS),
            ApprovalStep.subject_snapshot.is_not(None),
            sa.true() if include_withdrawn else model.withdrawn_at.is_(None),
        )
        .limit(200)
    ).all()
    out: list[tuple[str, dict[str, Any], str, str]] = []
    for label, snap, decision, withdrawn_at, rejected_before, sid in rows:
        if not isinstance(snap, dict):
            continue
        returned_only = (
            decision != ApprovalDecision.reject and withdrawn_at is not None and not rejected_before
        )
        kind = _REF_RETURNED if returned_only else _REF_REJECTED
        out.append((str(label or "untitled"), snap, kind, str(sid)))
    return out


WITHDRAWN_REPORT_ACTION = "expense_report.withdrawn"


POSSIBLE_REFILE_FLAG = "possible_refile"
EXPENSE_DELETED_ACTION = "expense.deleted"
# How a re-file flag was matched (``match`` on the flag): the line was moved out
# of the withdrawn report, it is backed by a receipt the reviewer saw there, or
# its content matches a frozen line.
_MATCH_LINEAGE = "lineage"
_MATCH_RECEIPT = "receipt"
_MATCH_CONTENT = "content"


@dataclass
class _WithdrawnRef:
    """One report the owner WITHDREW after a human return (phase 10 re-file check).

    ``entries``: every line frozen on its return steps and on its withdrawal row.
    ``wide``: match ALL those lines the pre-phase-10 way (amount / date /
    currency only, strong flag) — legacy only: a frozen line has no category
    (written before phase 10: a legacy move left no lineage), or a line was
    deleted after the report first entered review and its return snapshots do
    not list entry ids (so what the reviewer saw cannot be told apart).
    ``wide_entries``: the content of REVIEWED lines that were later deleted
    (their lineage is gone, so a re-typed copy must not get away with a new
    vendor): matched the wide way, strong flag.  See ``_withdrawn_report_refs``.
    Otherwise re-typed lines only match narrowly (``possible_refile``).
    """

    report_id: str
    title: str
    kind: str = _REF_RETURNED
    entries: list[dict[str, Any]] = field(default_factory=list)
    receipt_ids: set[str] = field(default_factory=set)
    entry_ids: set[str] = field(default_factory=set)
    wide: bool = False
    wide_entries: list[dict[str, Any]] = field(default_factory=list)
    # Lines listed on its human return snapshots (what a reviewer saw);
    # ``snapshot_ids_listed`` is False when any snapshot predates entry ids.
    reviewed_ids: set[str] = field(default_factory=set)
    snapshot_ids_listed: bool = False
    first_review_at: Any = None


_WITHDRAWN_REF_LIMIT = 200


def _withdrawn_report_refs(
    subject: ExpenseReport,
    lineage_ids: set[uuid.UUID],
    session: Session,
    released_line_ids: list[uuid.UUID] | None = None,
) -> dict[str, _WithdrawnRef]:
    """The owner's withdrawn returned / rejected reports, keyed by report id.

    The ``_WITHDRAWN_REF_LIMIT`` most recently withdrawn, plus every report a line
    of *subject* was released from (``lineage_ids``), plus every withdrawn report
    whose withdrawal snapshot lists one of ``released_line_ids`` — a line that
    travelled A → B → C keeps A as its origin, but a reject on B must still be
    seen.  ``kind`` is ``_REF_REJECTED`` when the report's history holds any
    reject (or it was rejected at withdrawal), else ``_REF_RETURNED``.
    """
    scope = sa.and_(
        ExpenseReport.organization_id == subject.organization_id,
        ExpenseReport.owner_id == subject.owner_id,
        ExpenseReport.id != subject.id,
        ExpenseReport.withdrawn_at.is_not(None),
    )
    rows = list(
        session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.title)
            .where(scope)
            .order_by(ExpenseReport.withdrawn_at.desc(), ExpenseReport.id)
            .limit(_WITHDRAWN_REF_LIMIT)
        ).all()
    )
    if released_line_ids:
        carried = sa.select(AuditLog.entity_id).where(
            AuditLog.organization_id == subject.organization_id,
            AuditLog.entity_type == "expense_report",
            AuditLog.action == WITHDRAWN_REPORT_ACTION,
            sa.or_(
                *(
                    AuditLog.after_state.contains({"entries": [{"entry_id": str(line_id)}]})
                    for line_id in released_line_ids
                )
            ),
        )
        rows += session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.title).where(
                scope, ExpenseReport.id.in_(carried)
            )
        ).all()
    if lineage_ids:
        rows += session.execute(
            sa.select(ExpenseReport.id, ExpenseReport.title).where(
                scope, ExpenseReport.id.in_(list(lineage_ids))
            )
        ).all()
    refs = {str(rid): _WithdrawnRef(str(rid), str(title or "untitled")) for rid, title in rows}
    if not refs:
        return refs
    ids = [uuid.UUID(rid) for rid in refs]
    legacy_snapshot: set[str] = set()
    for sid, decision, snap in session.execute(
        sa.select(Approval.subject_id, ApprovalStep.decision, ApprovalStep.subject_snapshot)
        .join(Approval, ApprovalStep.approval_id == Approval.id)
        .where(
            Approval.organization_id == subject.organization_id,
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id.in_(ids),
            ApprovalStep.decision.in_(_HUMAN_RETURN_DECISIONS),
        )
    ).all():
        ref = refs[str(sid)]
        if decision == ApprovalDecision.reject:
            ref.kind = _REF_REJECTED
        if not isinstance(snap, dict):
            legacy_snapshot.add(ref.report_id)  # a return that froze nothing
            continue
        ref.receipt_ids.update(str(x) for x in snap.get(SNAPSHOT_RECEIPTS_KEY) or [])
        for e in snap.get("entries") or []:
            if not isinstance(e, dict):
                continue
            ref.entries.append(e)
            if e.get("entry_id"):
                ref.reviewed_ids.add(str(e["entry_id"]))
            else:
                legacy_snapshot.add(ref.report_id)
    # When each report first entered review (its approval was created by the
    # first submission) — the database clock, like the audit rows below.
    for sid, first in session.execute(
        sa.select(Approval.subject_id, sa.func.min(Approval.created_at))
        .where(
            Approval.organization_id == subject.organization_id,
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id.in_(ids),
        )
        .group_by(Approval.subject_id)
    ).all():
        refs[str(sid)].first_review_at = first
    for ref in refs.values():
        ref.snapshot_ids_listed = ref.report_id not in legacy_snapshot
    for rid, state in session.execute(
        sa.select(AuditLog.entity_id, AuditLog.after_state).where(
            AuditLog.organization_id == subject.organization_id,
            AuditLog.entity_type == "expense_report",
            AuditLog.action == WITHDRAWN_REPORT_ACTION,
            AuditLog.entity_id.in_(ids),
        )
    ).all():
        if not isinstance(state, dict):
            continue
        ref = refs[str(rid)]
        if state.get("approval_status") == ApprovalStatus.rejected.value:
            ref.kind = _REF_REJECTED
        for e in state.get("entries") or []:
            if isinstance(e, dict):
                ref.entries.append(e)
                if e.get("entry_id"):
                    ref.entry_ids.add(str(e["entry_id"]))
    for ref in refs.values():
        ref.wide = any("category" not in e for e in ref.entries)
    # A deleted line's lineage is gone, so a re-typed copy of it must not get
    # away with a new vendor — but only a line a REVIEWER SAW counts (blocking
    # review finding: deleting a typo from a draft before it was ever submitted
    # made every later same-amount / same-date line a strong re-file):
    #   * the return snapshots list entry ids -> the deleted line must be one
    #     of them; only ITS content is matched wide (``wide_entries``);
    #   * legacy snapshots without entry ids -> the deletion must not be older
    #     than the report's first submission (database clock of the approval's
    #     creation vs the deletion's audit row: separate transactions; a tie
    #     counts, fail safe); then the whole report matches wide, as before.
    # Deletions before the first submission never count.
    released_ids = [uuid.UUID(e) for ref in refs.values() for e in ref.entry_ids]
    deleted_from = AuditLog.before_state["report_id"].astext
    for entity_id, from_report, deleted_at, before in session.execute(
        sa.select(
            AuditLog.entity_id, deleted_from, AuditLog.created_at, AuditLog.before_state
        ).where(
            AuditLog.organization_id == subject.organization_id,
            AuditLog.entity_type == "expense",
            AuditLog.action == EXPENSE_DELETED_ACTION,
            sa.or_(
                AuditLog.entity_id.in_(released_ids),
                deleted_from.in_(list(refs)),
            ),
        )
    ).all():
        line = str(entity_id)
        for ref in refs.values():
            if line not in ref.entry_ids and from_report != ref.report_id:
                continue
            if ref.snapshot_ids_listed:
                if line not in ref.reviewed_ids:
                    continue  # never in front of a reviewer
                ref.wide_entries.extend(
                    e for e in ref.entries if str(e.get("entry_id") or "") == line
                )
                if isinstance(before, dict) and before.get("amount") is not None:
                    ref.wide_entries.append(before)
            elif ref.first_review_at is not None and deleted_at >= ref.first_review_at:
                ref.wide = True
    return refs


def _norm_text(value: Any) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in str(value or "").lower()).split())


def _line_label(vendor: Any, description: Any) -> str:
    """Normalised vendor, else normalised description ("" when neither is set)."""
    return _norm_text(vendor) or _norm_text(description)


def _content_close(amount: Decimal, currency: Any, day: date, e: dict[str, Any]) -> bool:
    """Amount ±max($1, 2%), date ±3 days, same currency — the pre-phase-10 match."""
    e_day = _as_date(e.get("expense_date"))
    try:
        e_amount = Decimal(str(e.get("amount")))
    except (InvalidOperation, ValueError, TypeError):
        return False
    return (
        e_day is not None
        and str(e.get("currency")) == str(currency)
        and abs((day - e_day).days) <= _REFILE_ENTRY_DATE_DAYS
        and _amount_close(amount, e_amount)
    )


def _refiled_entry_flag(
    *,
    kind: str,
    match: str,
    vendor: Any,
    amount: Decimal,
    currency: Any,
    day: date,
    title: str,
    rid: str | None,
) -> dict[str, Any]:
    if match == _MATCH_LINEAGE:
        what = f"This line was moved out of report '{title}'"
    elif match == _MATCH_RECEIPT:
        what = f"This line is backed by a receipt a reviewer saw on report '{title}'"
    else:
        what = (
            f"A claim of about {amount:.2f} {currency} on {day.isoformat()} matches one "
            f"on report '{title}'"
        )
    if kind == _REF_REJECTED:
        flag: dict[str, Any] = {
            "flag_type": REFILED_REJECTED_FLAG,
            "reason": f"{what}, which a reviewer returned. Check it is not a re-filed claim.",
        }
    else:
        flag = {
            "flag_type": REFILED_RETURNED_FLAG,
            "reason": (
                f"{what}, which a reviewer sent back for changes and the submitter then "
                "withdrew. Review it as you would the original return."
            ),
        }
    flag.update({"severity": "blocking", "requires_approver": None, "entry_ref": vendor or "Entry"})
    flag["match"] = match
    if rid is not None:
        flag["related_subject_id"] = rid
    return flag


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _refiled_entry_flags(subject: ExpenseReport, session: Session) -> list[dict[str, Any]]:
    """Report half of ``_refiled_rejected_flags`` (phase 10: lineage first).

    Per line, the first hit wins:
      1. LINEAGE — the line was moved out of a withdrawn returned / rejected report
         (``released_from_report_id``, set by the move): strong flag, whatever the
         owner edited since;
      2. RECEIPT — the line is backed by a receipt a reviewer saw on such a report
         (its return snapshot's ``receipt_ids``): strong flag;
      3. CONTENT, wide (amount ±max($1, 2%), date ±3 days, same currency) against
         the owner's REJECTED reports and the frozen returns of subjects NOT
         withdrawn (unchanged), and against withdrawn reports that must stay
         wide (legacy rows), or against the content of a REVIEWED line that was
         later deleted: strong flag;
      4. CONTENT, narrow — a line re-typed from scratch that also has the same
         category and the same normalised vendor (else description) as a line of
         a withdrawn report: ``possible_refile`` (advisory — still escalated to a
         human, never blocking).
    """
    mine = session.execute(
        # Column order kept from the pre-phase-10 probe (amount first): the
        # hermetic fake sessions route this statement by its prefix.
        sa.select(
            Expense.amount,
            Expense.currency,
            Expense.expense_date,
            Expense.vendor,
            Expense.description,
            Expense.category,
            Expense.released_from_report_id,
            Expense.id,
        ).where(
            Expense.report_id == subject.id,
            Expense.organization_id == subject.organization_id,
        )
    ).all()
    if not mine:
        return []
    # (amount, currency, day, title, kind, report id)
    refs: list[tuple[Decimal, str, date, str, str, str]] = []
    for r_amount, r_currency, r_day, title, rid in session.execute(
        sa.select(
            Expense.amount,
            Expense.currency,
            Expense.expense_date,
            ExpenseReport.title,
            ExpenseReport.id,
        )
        .join(ExpenseReport, Expense.report_id == ExpenseReport.id)
        .where(
            ExpenseReport.organization_id == subject.organization_id,
            Expense.organization_id == subject.organization_id,
            ExpenseReport.owner_id == subject.owner_id,
            ExpenseReport.id != subject.id,
            ExpenseReport.status == ExpenseReportStatus.rejected,
        )
        .order_by(ExpenseReport.title)
        .limit(500)
    ).all():
        d = _as_date(r_day)
        if r_amount is not None and d is not None:
            refs.append(
                (Decimal(str(r_amount)), str(r_currency), d, str(title), _REF_REJECTED, str(rid))
            )
    for title, snap, kind, rid in _returned_snapshots(
        subject,
        ApprovalSubjectType.expense_report,
        subject.owner_id,
        session,
        include_withdrawn=False,
    ):
        for e in snap.get("entries") or []:
            d = _as_date(e.get("expense_date"))
            try:
                amt = Decimal(str(e.get("amount")))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if d is not None:
                refs.append((amt, str(e.get("currency")), d, title, kind, rid))
    # A rejected reference wins over a returned one for the same line.
    refs.sort(key=lambda ref: ref[4] != _REF_REJECTED)

    lineage_ids = {row[6] for row in mine if row[6] is not None}
    released_line_ids = [row[7] for row in mine if row[6] is not None]
    withdrawn = sorted(
        _withdrawn_report_refs(subject, lineage_ids, session, released_line_ids).values(),
        key=lambda ref: ref.kind != _REF_REJECTED,
    )
    by_id = {ref.report_id: ref for ref in withdrawn}
    receipts = receipt_ids_by_entry(
        session, organization_id=subject.organization_id, entry_ids=[row[7] for row in mine]
    )

    flags: list[dict[str, Any]] = []
    for amount, currency, day, vendor, description, category, released, line_id in mine:
        d_mine = _as_date(day)
        if amount is None or d_mine is None:
            continue
        a_mine = Decimal(str(amount))
        common = {"vendor": vendor, "amount": a_mine, "currency": currency, "day": d_mine}
        if released is not None:
            # Keep-first lineage names the origin, but a reject anywhere on the
            # line's path (any withdrawal snapshot listing it) wins: the origin
            # if it was rejected, else the first rejecting report.
            ref = by_id.get(str(released))
            if ref is None or ref.kind != _REF_REJECTED:
                ref = next(
                    (
                        w
                        for w in withdrawn
                        if w.kind == _REF_REJECTED and str(line_id) in w.entry_ids
                    ),
                    ref,
                )
            flags.append(
                _refiled_entry_flag(
                    kind=ref.kind if ref else _REF_RETURNED,
                    match=_MATCH_LINEAGE,
                    title=ref.title if ref else "a withdrawn report",
                    rid=ref.report_id if ref else str(released),
                    **common,
                )
            )
            continue
        line_receipts = {str(r) for r in receipts.get(line_id, [])}
        hit = next((ref for ref in withdrawn if line_receipts & ref.receipt_ids), None)
        if hit is not None:
            flags.append(
                _refiled_entry_flag(
                    kind=hit.kind,
                    match=_MATCH_RECEIPT,
                    title=hit.title,
                    rid=hit.report_id,
                    **common,
                )
            )
            continue
        wide = next(
            (
                ref
                for ref in refs
                if ref[1] == currency
                and abs((d_mine - ref[2]).days) <= _REFILE_ENTRY_DATE_DAYS
                and _amount_close(a_mine, ref[0])
            ),
            None,
        )
        if wide is not None:
            _r_amount, _r_currency, _r_day, title, kind, rid = wide
            flag = _refiled_entry_flag(
                kind=kind,
                match=_MATCH_CONTENT,
                title=title,
                rid=rid if kind == _REF_RETURNED else None,
                **common,
            )
            if kind == _REF_REJECTED:  # pre-phase-10 wording for a rejected claim
                flag["reason"] = (
                    f"A claim of about {a_mine:.2f} {currency} on {d_mine.isoformat()} "
                    f"matches one a reviewer returned on report '{title}'. Check it is not "
                    "a re-filed claim."
                )
            flags.append(flag)
            continue
        close = [
            ref
            for ref in withdrawn
            if any(_content_close(a_mine, currency, d_mine, e) for e in ref.entries)
        ]
        strong = next(
            (
                ref
                for ref in withdrawn
                # A REJECTED claim keeps the strong amount/date/currency match: retyping
                # it with another vendor/description must not slip past the narrow
                # label match (security review). Only returned (request-changes)
                # history gets the narrow vendor/category treatment.
                if ((ref.wide or ref.kind == _REF_REJECTED) and ref in close)
                or any(_content_close(a_mine, currency, d_mine, e) for e in ref.wide_entries)
            ),
            None,
        )
        if strong is not None:
            flags.append(
                _refiled_entry_flag(
                    kind=strong.kind,
                    match=_MATCH_CONTENT,
                    title=strong.title,
                    rid=strong.report_id,
                    **common,
                )
            )
            continue
        label = _line_label(vendor, description)
        category_value = getattr(category, "value", category)
        narrow = next(
            (
                ref
                for ref in close
                if any(
                    _content_close(a_mine, currency, d_mine, e)
                    and e.get("category") == category_value
                    and _line_label(e.get("vendor"), e.get("description")) == label
                    for e in ref.entries
                )
            ),
            None,
        )
        if narrow is not None:
            state = "rejected" if narrow.kind == _REF_REJECTED else "sent back for changes"
            flags.append(
                {
                    "flag_type": POSSIBLE_REFILE_FLAG,
                    "severity": "advisory",
                    "requires_approver": None,
                    "entry_ref": vendor or "Entry",
                    "related_subject_id": narrow.report_id,
                    "match": _MATCH_CONTENT,
                    "reason": (
                        f"A claim of about {a_mine:.2f} {currency} on {d_mine.isoformat()} "
                        f"looks like a line of report '{narrow.title}', which a reviewer "
                        f"{state} and the submitter then withdrew. It may be the same claim "
                        "typed again."
                    ),
                }
            )
    return flags


def _refiled_rejected_flags(
    subject: Trip | ExpenseReport,
    subject_type: ApprovalSubjectType,
    session: Session,
) -> list[dict[str, Any]]:
    """Approval-time backstop: does this submission re-file content a human turned down?

    A rejected subject is a closed outcome (no penalty on unrelated submissions), and the
    claimant may legitimately re-file it — so nothing is blocked at pre-submit.  But a
    re-filed copy must reach a human, never the auto-approver.  Reference content =
      * the CURRENT content of the owner's REJECTED reports / trips, and
      * the content FROZEN on every human reject / request_changes step of the owner's
        other subjects (``ApprovalStep.subject_snapshot``) — so editing a returned
        subject in place, then re-filing its original content, is still caught.
    Matching: report entry — same currency, date ±3 days (UAT2 review), amount ±
    max($1, 2%), vendor ignored; trip — same destination (``_destination_key``:
    however it was typed — "New York NY" ~ "New York" ~ "JFK"), dates overlapping
    ±1 day.
    A match is a blocking flag → escalate (a false positive costs a review, never a
    blocked submit).
    Product-gaps integration: content a reviewer sent back on a subject the owner
    then WITHDREW is matched the same way but flagged ``refiled_returned_claim``
    (naming the original return); a withdrawn subject with any reject in its
    history stays a rejected reference.
    Phase 10: report lines are matched against WITHDRAWN reports by lineage first
    (``_refiled_entry_flags``); trips keep the content match above.
    """
    if subject_type == ApprovalSubjectType.expense_report:
        assert isinstance(subject, ExpenseReport)
        return _refiled_entry_flags(subject, session)

    assert isinstance(subject, Trip)
    start = _as_date(subject.start_date)
    if start is None:
        return []
    end = _as_date(subject.end_date) or start
    window = timedelta(days=_REFILE_DATE_DAYS)
    dest = _destination_key(subject.primary_destination)
    # (destination, start, end, kind, trip id)
    trip_refs: list[tuple[str, date, date, str, str]] = []
    for r_dest, r_start, r_end in session.execute(
        sa.select(Trip.primary_destination, Trip.start_date, Trip.end_date).where(
            Trip.organization_id == subject.organization_id,
            Trip.traveler_id == subject.traveler_id,
            Trip.id != subject.id,
            Trip.status == TripStatus.rejected,
        )
    ).all():
        rs = _as_date(r_start)
        if rs is not None:
            trip_refs.append(
                # The subject id is only cited by a re-filed RETURNED flag.
                (str(r_dest or ""), rs, _as_date(r_end) or rs, _REF_REJECTED, "")
            )
    for _label, snap, kind, rid in _returned_snapshots(
        subject, subject_type, subject.traveler_id, session
    ):
        rs = _as_date(snap.get("start_date"))
        if rs is not None:
            trip_refs.append(
                (
                    str(snap.get("destination") or ""),
                    rs,
                    _as_date(snap.get("end_date")) or rs,
                    kind,
                    rid,
                )
            )
    trip_refs.sort(key=lambda ref: ref[3] != _REF_REJECTED)
    out: list[dict[str, Any]] = []
    for r_dest, r_start, r_end, kind, rid in trip_refs:
        if _destination_key(r_dest) == dest and r_start <= end + window and r_end >= start - window:
            if kind == _REF_REJECTED:
                out.append(
                    {
                        "flag_type": REFILED_REJECTED_FLAG,
                        "severity": "blocking",
                        "requires_approver": None,
                        "entry_ref": "Returned trip",
                        "reason": (
                            f"A trip to {r_dest} ({r_start.isoformat()}) that a reviewer "
                            "returned overlaps this one. Check it is not a re-filed trip."
                        ),
                    }
                )
            else:
                out.append(
                    {
                        "flag_type": REFILED_RETURNED_FLAG,
                        "severity": "blocking",
                        "requires_approver": None,
                        "entry_ref": "Withdrawn returned trip",
                        "related_subject_id": rid,
                        "reason": (
                            f"A trip to {r_dest} ({r_start.isoformat()}) that a reviewer sent "
                            "back for changes, and the traveler then withdrew, overlaps this "
                            "one. Review it as you would the original return."
                        ),
                    }
                )
            break
    return out


POSSIBLE_SPLIT_TRIP_FLAG = "possible_split_trip"
# Trips still "live" for the split check: awaiting a decision, approved, or completed
# (an approved claim that was spent — and the owner may PATCH approved -> completed
# at any time, so excluding it would let half 1 be "completed" early to hide it from
# half 2).  Drafts are not yet claims; cancelled trips are withdrawn; a rejected one
# is covered by the re-file check.
_LIVE_TRIP_STATUSES = (TripStatus.pending_approval, TripStatus.approved, TripStatus.completed)


def _destination_key(value: Any) -> str:
    """Same place, however it was typed: "JFK", "New York", "nyc", "New York NY" ->
    "JFK"; an unlisted city by its shared city key ("Austin TX" ~ "Austin, TX" ->
    "austin", ``config.constants.city_names``).  Coarser keys only ever ADD
    split-trip matches (escalations), never remove one."""
    text = str(value or "").strip()
    return resolve_airport(text) or primary_city_key(text) or text.lower()


def _possible_split_trip_flags(
    subject: Trip | ExpenseReport,
    subject_type: ApprovalSubjectType,
    session: Session,
) -> list[dict[str, Any]]:
    """UAT-2 Issue 4 (O1): one journey split across trips must reach a human.

    A trip whose dates overlap another LIVE trip (pending approval, approved or
    completed) of the same traveler to the same destination may be one journey
    filed as several trips — each under the auto-approve threshold.  Each such
    trip is a BLOCKING flag, so this one is escalated and never auto-approved.
    """
    if subject_type != ApprovalSubjectType.trip:
        return []
    assert isinstance(subject, Trip)
    start = _as_date(subject.start_date)
    if start is None:
        return []
    end = _as_date(subject.end_date) or start
    dest = _destination_key(subject.primary_destination)
    rows = session.execute(
        sa.select(
            Trip.id,
            Trip.origin,
            Trip.primary_destination,
            Trip.start_date,
            Trip.end_date,
            Trip.status,
        )
        .where(
            Trip.organization_id == subject.organization_id,
            Trip.traveler_id == subject.traveler_id,
            Trip.id != subject.id,
            Trip.status.in_(_LIVE_TRIP_STATUSES),
            Trip.start_date <= end,
            Trip.end_date >= start,
        )
        .order_by(Trip.start_date, Trip.id)
    ).all()
    out: list[dict[str, Any]] = []
    for tid, r_origin, r_dest, r_start, r_end, r_status in rows:
        if _destination_key(r_dest) != dest:
            continue
        status = r_status.value if hasattr(r_status, "value") else str(r_status)
        state = {"approved": "approved", "completed": "completed"}.get(status, "awaiting approval")
        out.append(
            {
                "flag_type": POSSIBLE_SPLIT_TRIP_FLAG,
                "severity": "blocking",
                "requires_approver": None,
                "entry_ref": "Overlapping trip",
                "related_subject_id": str(tid),
                "reason": (
                    f"This trip overlaps another trip by the same traveler to the same "
                    f"destination ({r_origin} → {r_dest}, {_as_date(r_start)} – "
                    f"{_as_date(r_end)}, {state}). They may be one journey split into "
                    "separate trips; review their costs together."
                ),
            }
        )
    return out


def assess_risk(
    subject: Trip | ExpenseReport,
    subject_type: ApprovalSubjectType,
    *,
    context: AgentContext,
    session: Session,
) -> RiskAssessment:
    """Build a RiskAssessment for one approval decision.

    Expense reports: ALWAYS re-runs the deterministic pre-submit policy
    evaluation (PolicyEvalTool via presubmit.run_presubmit) over the report's
    current entries and merges it with any stored pre_submit_flags (C7-05) —
    reports created via the API / manual paths have no stored flags, so trusting
    those alone let out-of-policy reports auto-approve.

    Trips: compliant only when every segment is explicitly in policy AND a
    fresh PolicyEvalTool run over the declared segments is clean (the stored
    in_policy / estimated_cost values are client-supplied via POST /trips).
    """
    rationale: tuple[str, ...]
    if subject_type == ApprovalSubjectType.expense_report:
        assert isinstance(subject, ExpenseReport)
        submitter_id: uuid.UUID = subject.owner_id
        (
            in_policy,
            blocking_count,
            requires_approver,
            total,
            flags,
            compliant,
            rationale,
        ) = _assess_expense_report(subject, context=context, session=session)
    else:
        assert isinstance(subject, Trip)
        submitter_id = subject.traveler_id
        (
            in_policy,
            blocking_count,
            requires_approver,
            total,
            flags,
            compliant,
            rationale,
        ) = _assess_trip(subject, session, context=context)

    # C7R SoD — never auto-approve while the owner has a returned, unresolved subject of the
    # same type (the reason is carried by the flag; not repeated in policy_rationale).
    returned = [
        *_open_returned_subject_flags(subject, subject_type, session),
        *_refiled_rejected_flags(subject, subject_type, session),
        *_possible_split_trip_flags(subject, subject_type, session),
    ]
    if returned:
        flags = [*flags, *returned]
        # Phase 10: an advisory review flag (``possible_refile``) still keeps the
        # subject from auto-approving but is not counted as blocking.
        blocking = sum(1 for fl in returned if fl.get("severity") == "blocking")
        blocking_count += blocking
        compliant = False
        # QA round: a blocking review flag means the subject is not cleared — the
        # summary must never read "in policy" beside a non-zero blocking count.
        if blocking:
            in_policy = False
    if subject_type == ApprovalSubjectType.expense_report:
        assert isinstance(subject, ExpenseReport)
        removed = _removed_since_return_flags(subject, session)
        if removed:
            # Informational for the reviewer; never auto-approved either way.
            flags = [*flags, *removed]
            compliant = False

    manager_id = _resolve_manager(submitter_id, session)

    return RiskAssessment(
        subject_type=subject_type,
        subject_id=subject.id,
        submitter_id=submitter_id,
        manager_id=manager_id,
        subject_in_policy=in_policy,
        blocking_flag_count=blocking_count,
        requires_approver=requires_approver,
        subject_total=total,
        flags_snapshot=flags,
        is_policy_compliant=compliant,
        policy_rationale=rationale,
    )


def build_risk_summary(assessment: RiskAssessment, *, route: str) -> dict[str, Any]:
    """Serialise a RiskAssessment to a JSONB-safe dict for Approval.risk_summary.

    Args:
        assessment: The computed risk assessment.
        route: "auto_approve" or "escalate".
    """
    summary: dict[str, Any] = {
        "route": route,
        "subject_type": assessment.subject_type.value,
        "subject_in_policy": assessment.subject_in_policy,
        "is_policy_compliant": assessment.is_policy_compliant,
        "blocking_flag_count": assessment.blocking_flag_count,
        "requires_approver": assessment.requires_approver,
        "subject_total_usd": assessment.subject_total,
        "flags": assessment.flags_snapshot,
    }
    if assessment.policy_rationale:
        summary["policy_rationale"] = list(assessment.policy_rationale)
    if route == "escalate":
        summary["routed_to_approver_id"] = (
            str(assessment.manager_id) if assessment.manager_id else None
        )
    return summary
