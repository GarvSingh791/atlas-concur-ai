"""Open approvals for subjects submitted before submit-opens-approval existed (C7R UAT).

Before the C7R approval-routing fix, submitting an expense report (``submitted``)
or a trip (``pending_approval``) did NOT create an approval — only POST
/approvals did, and no screen called it.  Such subjects sit in a database
forever with no approval a reviewer can act on.  This entry point repairs them
without touching anything else:

* every report still ``submitted`` / trip still ``pending_approval`` with NO
  approval row is put through the SAME path a submit now uses
  (``lifecycle.open_approval_on_submission`` with the owner as actor): the
  deterministic Approval Agent auto-approves or escalates it to an independent
  approver, with its usual audit + decision-log rows; an auto-approval moves the
  subject to ``approved`` through its own workflow state machine;
* every subject still in its submitted state under an approval that a human (or
  the agent) already DECIDED is moved to that decision's outcome
  (``seed_all._reconcile_decided_subjects`` — e.g. the old "Q2 Summit Expenses"
  ``submitted``-under-``approved`` mismatch that dropped it from finance KPIs).

Idempotent: a re-run with nothing stranded is a no-op.  No LLM / network calls.
Each subject is repaired in its own transaction, so one failure never blocks
the rest (it is logged and counted).

Run: ``python -m database.seeds.backfill_submitted_approvals`` (exit 0 also when
nothing needs repair; exit 1 when any subject failed).  Wired into the
``SEED_ON_STARTUP=policy_corpus`` boot path (staging) after the card-feed top-up.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402

from backend.app.core.database import get_sessionmaker  # noqa: E402
from database.models.approval import Approval, ApprovalStatus, ApprovalSubjectType  # noqa: E402
from database.models.expense_report import ExpenseReport, ExpenseReportStatus  # noqa: E402
from database.models.identity import Organization, User  # noqa: E402
from database.models.trip import Trip, TripStatus  # noqa: E402

logger = logging.getLogger(__name__)


def _stranded(session: sa.orm.Session) -> list[tuple[ApprovalSubjectType, Any]]:
    """(subject_type, id) of submitted subjects that have NO approval row at all."""
    has_report_approval = (
        sa.select(Approval.id)
        .where(
            Approval.subject_type == ApprovalSubjectType.expense_report,
            Approval.subject_id == ExpenseReport.id,
            Approval.organization_id == ExpenseReport.organization_id,
        )
        .exists()
    )
    has_trip_approval = (
        sa.select(Approval.id)
        .where(
            Approval.subject_type == ApprovalSubjectType.trip,
            Approval.subject_id == Trip.id,
            Approval.organization_id == Trip.organization_id,
        )
        .exists()
    )
    reports = session.scalars(
        sa.select(ExpenseReport.id)
        .where(ExpenseReport.status == ExpenseReportStatus.submitted, ~has_report_approval)
        .order_by(ExpenseReport.created_at, ExpenseReport.id)
    ).all()
    trips = session.scalars(
        sa.select(Trip.id)
        .where(Trip.status == TripStatus.pending_approval, ~has_trip_approval)
        .order_by(Trip.created_at, Trip.id)
    ).all()
    return [(ApprovalSubjectType.expense_report, r) for r in reports] + [
        (ApprovalSubjectType.trip, t) for t in trips
    ]


def _open_one(
    session: sa.orm.Session, subject_type: ApprovalSubjectType, subject_id: object
) -> str:
    # Imported lazily: these pull in the backend service graph.
    from backend.app.modules.approval.lifecycle import open_approval_on_submission
    from backend.app.modules.expense.service import apply_workflow_transition
    from backend.app.modules.travel.service import apply_workflow_status

    model: Any = ExpenseReport if subject_type == ApprovalSubjectType.expense_report else Trip
    subject: Any = session.scalars(
        sa.select(model).where(model.id == subject_id).with_for_update()
    ).first()
    if subject is None:
        return "gone"
    owner_id = subject.owner_id if isinstance(subject, ExpenseReport) else subject.traveler_id
    owner = session.get(User, owner_id)
    if owner is None:
        return "no-owner"
    approval = open_approval_on_submission(
        session,
        organization_id=subject.organization_id,
        subject_type=subject_type,
        subject_id=subject.id,
        actor=owner,
    )
    if approval.current_status == ApprovalStatus.auto_approved:
        if isinstance(subject, ExpenseReport):
            apply_workflow_transition(session, subject, ExpenseReportStatus.approved, actor_id=None)
        elif isinstance(subject, Trip):
            apply_workflow_status(session, subject, TripStatus.approved, actor_id=None)
    return approval.current_status.value


def backfill(session_factory=None) -> dict[str, int]:  # type: ignore[no-untyped-def]
    if session_factory is None:
        session_factory = get_sessionmaker()
    opened = failed = reconciled = 0
    with session_factory() as session:
        targets = _stranded(session)
    for subject_type, subject_id in targets:
        with session_factory() as session:
            try:
                outcome = _open_one(session, subject_type, subject_id)
                session.commit()
                opened += 1
                logger.info(
                    "Opened approval for %s %s -> %s", subject_type.value, subject_id, outcome
                )
            except Exception:
                session.rollback()
                failed += 1
                logger.exception(
                    "Could not open an approval for %s %s", subject_type.value, subject_id
                )

    from database.seeds.seed_all import _reconcile_decided_subjects

    with session_factory() as session:
        try:
            for org in session.scalars(sa.select(Organization)).all():
                reconciled += _reconcile_decided_subjects(session, org)
            session.commit()
        except Exception:
            session.rollback()
            failed += 1
            logger.exception("Reconciling decided subjects failed (rolled back).")
    logger.info(
        "Approval backfill: %d opened, %d reconciled, %d failed.", opened, reconciled, failed
    )
    return {"opened": opened, "reconciled": reconciled, "failed": failed}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        result = backfill()
    except Exception:
        logger.exception("Approval backfill FAILED.")
        return 1
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
