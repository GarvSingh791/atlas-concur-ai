"""Top up the demo corporate-card feed — safe to run against a live database (C7-22).

Smart-match pairs a receipt with an UNMATCHED corporate-card transaction.  The
demo org's employee has exactly three seeded card swipes (one per demo receipt
template: United Airlines 320.00 on 2026-06-10, New York Marriott Marquis 956.00
on 2026-06-10, Joe's Pizza NYC 38.50 on 2026-06-11).  A successful match sets
``CardTransaction.matched`` / ``expense_id`` permanently, there is no API to
create card transactions, and the full ``seed_all`` only checks that a row with
the same merchant/date/amount EXISTS — so after one acceptance run the feed is
exhausted and smart-match can never be demonstrated again on staging.

This entry point restores the feed without touching anything else: for each
demo template it ensures at least one UNMATCHED transaction (``matched`` false
AND no ``expense_id``) exists for the demo employee, creating a fresh one only
when every existing copy has been consumed.  Consumed rows are never modified
(they stay linked to their expenses for audit).  It touches no users,
passwords, trips, reports, receipts or approvals.

Idempotent: a re-run with the feed available is a no-op.  No network calls.

Duplicate claims (C7R): a fresh swipe lets the SAME demo receipt be uploaded and
smart-matched again after its first report was approved, i.e. one purchase
claimed twice with card corroboration.  That is intentional for re-running the
demo, and it is not a policy bypass: every policy path (approval-time
re-evaluation, POST /policy/evaluate) runs ``presubmit.duplicate_claim_issue``,
which raises a BLOCKING ``possible_duplicate`` flag when the owner already has
the same vendor + date + amount + currency on another report that was not rejected —
so such a report is always escalated to a human, never auto-approved.

Run: ``python -m database.seeds.topup_demo_card_feed`` (exit 0 also when the
demo org/employee does not exist; exit 1 on failure, transaction rolled back).
Wired into the ``SEED_ON_STARTUP=policy_corpus`` boot path (staging) after the
policy-corpus reindex.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlalchemy as sa  # noqa: E402

from backend.app.core.database import get_sessionmaker  # noqa: E402
from database.models.card_transaction import CardTransaction  # noqa: E402
from database.models.identity import Organization, User  # noqa: E402

logger = logging.getLogger(__name__)

DEMO_ORG_NAME = "Atlas Demo Corp"
DEMO_EMPLOYEE_EMAIL = "employee@atlas-demo.internal"


@dataclass(frozen=True)
class DemoCardTemplate:
    """One demo card swipe; matches a demo receipt by merchant/amount/date."""

    merchant: str
    txn_date: date
    amount: Decimal
    currency: str = "USD"
    last_four: str = "4242"


# Single source of truth for the demo card feed (also used by seed_all).
DEMO_CARD_TRANSACTIONS: tuple[DemoCardTemplate, ...] = (
    DemoCardTemplate("United Airlines", date(2026, 6, 10), Decimal("320.00")),
    DemoCardTemplate("New York Marriott Marquis", date(2026, 6, 10), Decimal("956.00")),
    DemoCardTemplate("Joe's Pizza NYC", date(2026, 6, 11), Decimal("38.50")),
)


def _template_filter(user_id: object, tpl: DemoCardTemplate) -> list[sa.ColumnElement[bool]]:
    return [
        CardTransaction.user_id == user_id,
        CardTransaction.merchant == tpl.merchant,
        CardTransaction.txn_date == tpl.txn_date,
        CardTransaction.amount == tpl.amount,
        CardTransaction.currency == tpl.currency,
    ]


def top_up(session_factory=None) -> dict[str, int] | None:  # type: ignore[no-untyped-def]
    """Ensure one unmatched demo card transaction per template.

    Returns ``{"created": n, "available": m}`` (m = templates that already had an
    unmatched copy), or None when the demo org/employee does not exist.
    """
    if session_factory is None:
        session_factory = get_sessionmaker()
    with session_factory() as session:
        org = session.query(Organization).filter(Organization.name == DEMO_ORG_NAME).first()
        if org is None:
            logger.warning("Demo org %r not found — no card feed to top up.", DEMO_ORG_NAME)
            return None
        employee = (
            session.query(User)
            .filter(User.organization_id == org.id, User.email == DEMO_EMPLOYEE_EMAIL)
            .first()
        )
        if employee is None:
            logger.warning("Demo employee not found — no card feed to top up.")
            return None
        created = available = 0
        try:
            for tpl in DEMO_CARD_TRANSACTIONS:
                unmatched = session.scalar(
                    sa.select(CardTransaction.id)
                    .where(
                        *_template_filter(employee.id, tpl),
                        CardTransaction.organization_id == org.id,
                        CardTransaction.matched.is_(False),
                        CardTransaction.expense_id.is_(None),
                    )
                    .limit(1)
                )
                if unmatched is not None:
                    available += 1
                    continue
                session.add(
                    CardTransaction(
                        organization_id=org.id,
                        user_id=employee.id,
                        expense_id=None,
                        amount=tpl.amount,
                        currency=tpl.currency,
                        merchant=tpl.merchant,
                        txn_date=tpl.txn_date,
                        last_four=tpl.last_four,
                        matched=False,
                    )
                )
                created += 1
            session.commit()
        except Exception:
            session.rollback()
            raise
    logger.info("Demo card feed topped up: %d created, %d already available.", created, available)
    return {"created": created, "available": available}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        top_up()
    except Exception:
        logger.exception("Demo card feed top-up FAILED (rolled back).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
