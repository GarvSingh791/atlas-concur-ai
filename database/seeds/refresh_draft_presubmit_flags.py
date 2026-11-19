"""Re-evaluate every DRAFT report's stored pre-submit flags (Issue 6).

``expense_reports.pre_submit_flags`` is a SNAPSHOT: it is rebuilt only when an
entry of the report changes (``refresh_presubmit_flags`` in the expense
service).  After a policy-figure correction (migration
0015_canonical_policy_figures: HOTEL-CAP / MEALS-PD now follow the canonical
TEP-FY26 caps) a draft report keeps showing the OLD caps ("$180 domestic cap")
until its owner happens to edit an entry.  This entry point re-runs that same
refresh for every draft report so the readiness panel and the submit gate show
what the engine says today.

Only ``draft`` reports are touched — submitted / approved / rejected reports keep
the snapshot they were decided on (append-only decision history).  Idempotent:
a re-run recomputes the same flags.  No LLM / network calls.  Each report is
refreshed in its own transaction, so one failure never blocks the rest (it is
logged and counted).

Run: ``python -m database.seeds.refresh_draft_presubmit_flags`` (exit 0 also when
there are no drafts; exit 1 when any report failed).  Wired into the
``SEED_ON_STARTUP=policy_corpus`` boot path (staging) after the approval
backfill.
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
from database.models.expense_report import ExpenseReport, ExpenseReportStatus  # noqa: E402

logger = logging.getLogger(__name__)


def _draft_report_ids(session: sa.orm.Session) -> list[Any]:
    return list(
        session.scalars(
            sa.select(ExpenseReport.id)
            .where(ExpenseReport.status == ExpenseReportStatus.draft)
            .order_by(ExpenseReport.created_at, ExpenseReport.id)
        ).all()
    )


def refresh(session_factory=None) -> dict[str, int]:  # type: ignore[no-untyped-def]
    """Refresh every draft report's pre_submit_flags; returns {refreshed, failed}."""
    # Imported lazily: pulls in the backend service graph.
    from backend.app.modules.expense.service import refresh_presubmit_flags

    if session_factory is None:
        session_factory = get_sessionmaker()
    refreshed = failed = 0
    with session_factory() as session:
        report_ids = _draft_report_ids(session)
    for report_id in report_ids:
        with session_factory() as session:
            try:
                report = session.scalars(
                    sa.select(ExpenseReport)
                    .where(
                        ExpenseReport.id == report_id,
                        ExpenseReport.status == ExpenseReportStatus.draft,
                    )
                    .with_for_update()
                ).first()
                if report is None:  # submitted / deleted since the listing
                    continue
                refresh_presubmit_flags(session, report)
                session.commit()
                refreshed += 1
            except Exception:
                session.rollback()
                failed += 1
                logger.exception("Could not refresh pre-submit flags of report %s", report_id)
    logger.info("Draft pre-submit refresh: %d refreshed, %d failed.", refreshed, failed)
    return {"refreshed": refreshed, "failed": failed}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        result = refresh()
    except Exception:
        logger.exception("Draft pre-submit refresh FAILED.")
        return 1
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
