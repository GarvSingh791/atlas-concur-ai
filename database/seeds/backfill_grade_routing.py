"""Route PENDING approvals that predate approver-grade routing (follow-up, grades).

``grade_routing.route_by_grade`` only runs from ``lifecycle.ensure_independent_approver``
(submit / escalate / re-queue).  An approval that was already pending when it shipped
and carries a flag naming an approver grade (``requires_approver`` SVP / VP) was never
routed by grade: its line manager is below the grade (403 INSUFFICIENT_APPROVER_GRADE)
and the qualified approver higher up the chain cannot see it (``approval_scope`` only
widens when ``risk_summary`` has a ``grade_routing`` key).  This entry point repairs
exactly those:

* every PENDING approval whose flags name a ladder grade and whose ``risk_summary`` has
  NO ``grade_routing`` key is run through ``route_by_grade`` (the same function a
  submit uses), which writes ``required_approver_grade`` / ``grade_routing`` /
  ``routed_to_approver_id`` and audits a changed route (``approval.routed_by_grade``);
  a ``approval.grade_routing_backfilled`` audit row records the repair itself.

Idempotent: ``route_by_grade`` writes ``grade_routing``, so a re-run selects nothing.
Decided approvals and approvals naming no grade are never touched.  No LLM / network
calls.  Each approval is repaired in its own transaction, so one failure never blocks
the rest (it is logged and counted).

Run: ``python -m database.seeds.backfill_grade_routing`` (exit 0 also when nothing needs
repair; exit 1 when any approval failed).  Wired into the ``SEED_ON_STARTUP=policy_corpus``
boot path (staging) after the approval backfill, as a non-fatal repair.
"""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402

from backend.app.core.audit import record_audit  # noqa: E402
from backend.app.core.database import get_sessionmaker  # noqa: E402
from backend.app.modules.approval.grade_routing import (  # noqa: E402
    required_grade,
    route_by_grade,
)
from database.models.approval import Approval, ApprovalStatus  # noqa: E402

logger = logging.getLogger(__name__)


def _needs_routing(approval: Approval) -> bool:
    summary = approval.risk_summary if isinstance(approval.risk_summary, dict) else {}
    if "grade_routing" in summary:
        return False
    need, _ = required_grade(summary.get("flags"))
    return need is not None


def _stranded(session: sa.orm.Session) -> list[uuid.UUID]:
    """Ids of pending approvals that name an approver grade but were never routed by it."""
    rows = session.scalars(
        sa.select(Approval)
        .where(
            Approval.current_status == ApprovalStatus.pending,
            sa.not_(Approval.risk_summary.has_key("grade_routing")),
        )
        .order_by(Approval.created_at, Approval.id)
    ).all()
    return [a.id for a in rows if _needs_routing(a)]


def _route_one(session: sa.orm.Session, approval_id: uuid.UUID) -> str:
    approval = session.scalars(
        sa.select(Approval).where(Approval.id == approval_id).with_for_update()
    ).first()
    if approval is None or approval.current_status != ApprovalStatus.pending:
        return "gone"
    if not _needs_routing(approval):
        return "skipped"
    route_by_grade(session, approval)
    summary = approval.risk_summary or {}
    record_audit(
        session,
        organization_id=approval.organization_id,
        action="approval.grade_routing_backfilled",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=None,
        before_state=None,
        after_state={
            "required_approver_grade": summary.get("required_approver_grade"),
            "routed_to_approver_id": summary.get("routed_to_approver_id"),
            "routed_by": (summary.get("grade_routing") or {}).get("routed_by"),
        },
    )
    session.flush()
    return str((summary.get("grade_routing") or {}).get("routed_by"))


def backfill(session_factory=None) -> dict[str, int]:  # type: ignore[no-untyped-def]
    if session_factory is None:
        session_factory = get_sessionmaker()
    routed = failed = 0
    with session_factory() as session:
        targets = _stranded(session)
    for approval_id in targets:
        with session_factory() as session:
            try:
                outcome = _route_one(session, approval_id)
                session.commit()
                routed += 1
                logger.info("Routed approval %s by grade -> %s", approval_id, outcome)
            except Exception:
                session.rollback()
                failed += 1
                logger.exception("Could not route approval %s by grade", approval_id)
    logger.info("Grade-routing backfill: %d routed, %d failed.", routed, failed)
    return {"routed": routed, "failed": failed}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        result = backfill()
    except Exception:
        logger.exception("Grade-routing backfill FAILED.")
        return 1
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
