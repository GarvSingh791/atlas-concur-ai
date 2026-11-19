"""P6-T3.2 Lineage + orphan-integrity tests.

Proves the connected-domain lineage chain is intact end-to-end and that no FK
links are broken anywhere in the database.

Chain under test:
  receipts.expense_id ──────────► expenses.id            (SET NULL · NULLABLE)
  expenses.report_id ───────────► expense_reports.id     (CASCADE  · NOT NULL)
  expense_reports.trip_id ──────► trips.id               (SET NULL · NULLABLE)
  trip_segments.trip_id ────────► trips.id               (CASCADE  · NOT NULL)
    smart-match cross-link:
  expenses.trip_segment_id ─────► trip_segments.id       (SET NULL · NULLABLE)

Live-DB — self-skips when Postgres is unreachable.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy import Table
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from backend.app.core.database import get_engine, get_sessionmaker
from backend.app.core.security import hash_password
from database.models import Base
from database.models.audit_log import AuditLog
from database.models.card_transaction import CardTransaction
from database.models.expense import Expense, ExpenseCategory, ExpenseStatus
from database.models.expense_report import ExpenseReport, ExpenseReportStatus
from database.models.identity import (
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)
from database.models.receipt import OcrStatus, Receipt
from database.models.trip import Trip, TripStatus
from database.models.trip_segment import SegmentType, TripSegment
from database.seeds.identity_seed import seed_rbac_catalog

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# Scoped tables for create/drop
# ---------------------------------------------------------------------------

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Role.__table__),
    cast(Table, Permission.__table__),
    cast(Table, RolePermission.__table__),
    cast(Table, AuditLog.__table__),
    cast(Table, Trip.__table__),
    cast(Table, TripSegment.__table__),
    cast(Table, ExpenseReport.__table__),
    cast(Table, Expense.__table__),
    cast(Table, Receipt.__table__),
    cast(Table, CardTransaction.__table__),
]

# ---------------------------------------------------------------------------
# Unique email / name constants (isolated from other test modules)
# ---------------------------------------------------------------------------

_ORG_NAME = "T3-Lineage-Org"
_USER_EMAIL = "t3-lineage-user@test.internal"
_TEST_PW = "test-password-t3-lineage"

# ---------------------------------------------------------------------------
# DB fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def db_engine() -> Iterator[Engine]:
    """Yield the engine if Postgres is reachable, else skip the module."""
    engine = get_engine()
    try:
        conn = engine.connect()
        conn.close()
    except Exception:
        pytest.skip("Postgres not reachable — skipping T3 lineage tests")

    existing = set(sa_inspect(engine).get_table_names())
    tables_to_create = [t for t in _TABLES if t.name not in existing]
    Base.metadata.create_all(bind=engine, tables=tables_to_create)
    try:
        yield engine
    finally:
        for table in reversed(tables_to_create):
            table.drop(bind=engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Seed fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(db_engine: Engine) -> Iterator[dict[str, Any]]:
    """Seed RBAC + the full lineage chain + unmatched records for NULL assertions."""
    factory = get_sessionmaker()

    # Prune stale data from a previous incomplete run (FK-safe order).
    with factory() as session:
        stale = session.query(User).filter(User.email == _USER_EMAIL).all()
        stale_org_ids = {u.organization_id for u in stale}
        if stale_org_ids:
            session.query(AuditLog).filter(AuditLog.organization_id.in_(stale_org_ids)).delete()
            session.query(CardTransaction).filter(
                CardTransaction.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Receipt).filter(Receipt.organization_id.in_(stale_org_ids)).delete()
            session.query(Expense).filter(Expense.organization_id.in_(stale_org_ids)).delete()
            session.query(ExpenseReport).filter(
                ExpenseReport.organization_id.in_(stale_org_ids)
            ).delete()
            session.query(Trip).filter(Trip.organization_id.in_(stale_org_ids)).delete()
            session.query(User).filter(User.organization_id.in_(stale_org_ids)).delete()
            session.query(Organization).filter(Organization.id.in_(stale_org_ids)).delete()
            session.commit()

    with factory() as session:
        seed_rbac_catalog(session)

        # ------------------------------------------------------------------ #
        # Step 1: Org + user
        # ------------------------------------------------------------------ #
        org = Organization(name=_ORG_NAME, base_currency="USD")
        session.add(org)
        session.flush()

        user = User(
            organization_id=org.id,
            name="T3 Lineage User",
            email=_USER_EMAIL,
            role=RoleKey.employee,
            manager_id=None,
            status=UserStatus.active,
            password_hash=hash_password(_TEST_PW),
        )
        session.add(user)
        session.flush()

        # ------------------------------------------------------------------ #
        # Step 2: Lineage chain (all linked)
        # ------------------------------------------------------------------ #
        trip = Trip(
            organization_id=org.id,
            traveler_id=user.id,
            status=TripStatus.completed,
            origin="SFO",
            primary_destination="NYC",
            start_date=datetime.date(2026, 6, 1),
            end_date=datetime.date(2026, 6, 5),
            currency="USD",
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(trip)
        session.flush()

        segment = TripSegment(
            trip_id=trip.id,
            segment_type=SegmentType.flight,
            supplier="United Airlines",
            currency="USD",
            amount=Decimal("320.00"),
        )
        session.add(segment)
        session.flush()

        report = ExpenseReport(
            organization_id=org.id,
            owner_id=user.id,
            title="T3 Lineage Report",
            status=ExpenseReportStatus.approved,
            currency="USD",
            total_amount=Decimal("320.00"),
            trip_id=trip.id,
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(report)
        session.flush()

        expense = Expense(
            organization_id=org.id,
            report_id=report.id,
            category=ExpenseCategory.airfare,
            amount=Decimal("320.00"),
            currency="USD",
            expense_date=datetime.date(2026, 6, 1),
            status=ExpenseStatus.approved,
            trip_segment_id=segment.id,
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(expense)
        session.flush()

        receipt = Receipt(
            organization_id=org.id,
            uploaded_by=user.id,
            image_url="s3://test-bucket/t3-lineage-receipt.jpg",
            ocr_status=OcrStatus.pending,
            expense_id=expense.id,
        )
        session.add(receipt)
        session.flush()

        card_txn = CardTransaction(
            organization_id=org.id,
            user_id=user.id,
            merchant="United Airlines",
            amount=Decimal("320.00"),
            currency="USD",
            txn_date=datetime.date(2026, 6, 1),
            expense_id=expense.id,
        )
        session.add(card_txn)
        session.flush()

        # ------------------------------------------------------------------ #
        # Step 3: Unmatched records (expected NULLs)
        # ------------------------------------------------------------------ #
        unmatched_receipt = Receipt(
            organization_id=org.id,
            uploaded_by=user.id,
            image_url="s3://test-bucket/t3-lineage-unmatched.jpg",
            ocr_status=OcrStatus.pending,
            expense_id=None,
        )
        session.add(unmatched_receipt)
        session.flush()

        unmatched_report = ExpenseReport(
            organization_id=org.id,
            owner_id=user.id,
            title="T3 Unmatched Report",
            status=ExpenseReportStatus.draft,
            currency="USD",
            total_amount=Decimal("0.00"),
            trip_id=None,
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(unmatched_report)
        session.flush()

        unmatched_expense = Expense(
            organization_id=org.id,
            report_id=unmatched_report.id,
            category=ExpenseCategory.meals,
            amount=Decimal("45.00"),
            currency="USD",
            expense_date=datetime.date(2026, 6, 2),
            status=ExpenseStatus.draft,
            trip_segment_id=None,
            created_by=user.id,
            updated_by=user.id,
        )
        session.add(unmatched_expense)
        session.flush()

        unmatched_card_txn = CardTransaction(
            organization_id=org.id,
            user_id=user.id,
            merchant="Coffee Shop",
            amount=Decimal("5.00"),
            currency="USD",
            txn_date=datetime.date(2026, 6, 3),
            expense_id=None,
        )
        session.add(unmatched_card_txn)
        session.flush()

        session.commit()

    yield {
        "org_id": org.id,
        "user_id": user.id,
        "trip_id": trip.id,
        "segment_id": segment.id,
        "report_id": report.id,
        "expense_id": expense.id,
        "receipt_id": receipt.id,
        "card_txn_id": card_txn.id,
        "unmatched_receipt_id": unmatched_receipt.id,
        "unmatched_report_id": unmatched_report.id,
        "unmatched_expense_id": unmatched_expense.id,
        "unmatched_card_txn_id": unmatched_card_txn.id,
    }

    # Teardown — FK-safe order
    with factory() as session:
        org_ids = [org.id]
        session.query(AuditLog).filter(AuditLog.organization_id.in_(org_ids)).delete()
        session.query(CardTransaction).filter(CardTransaction.organization_id.in_(org_ids)).delete()
        session.query(Receipt).filter(Receipt.organization_id.in_(org_ids)).delete()
        session.query(Expense).filter(Expense.organization_id.in_(org_ids)).delete()
        session.query(ExpenseReport).filter(ExpenseReport.organization_id.in_(org_ids)).delete()
        session.query(Trip).filter(Trip.organization_id.in_(org_ids)).delete()
        session.query(User).filter(User.organization_id.in_(org_ids)).delete()
        session.query(Organization).filter(Organization.id.in_(org_ids)).delete()
        session.commit()


# ---------------------------------------------------------------------------
# TestLineageChain: prove the fully-linked chain resolves end-to-end
# ---------------------------------------------------------------------------


class TestLineageChain:
    def test_full_chain_receipt_to_segment(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Full chain receipt → expense → report → trip → segment resolves end-to-end."""
        factory = get_sessionmaker()
        with factory() as session:
            result = session.execute(
                sa.text("""
                    SELECT
                        r.id         AS receipt_id,
                        e.id         AS expense_id,
                        er.id        AS report_id,
                        t.id         AS trip_id,
                        ts_match.id  AS smart_match_segment_id
                    FROM receipts r
                    JOIN  expenses e             ON r.expense_id = e.id
                    JOIN  expense_reports er     ON e.report_id = er.id
                    LEFT  JOIN trips t           ON er.trip_id = t.id
                    LEFT  JOIN trip_segments ts_match ON e.trip_segment_id = ts_match.id
                    WHERE r.id = :receipt_id
                """),
                {"receipt_id": str(seeded["receipt_id"])},
            ).fetchall()

        assert len(result) == 1, "Expected exactly one chain row for the fixture receipt"
        row = result[0]
        assert str(row.receipt_id) == str(seeded["receipt_id"])
        assert str(row.expense_id) == str(seeded["expense_id"])
        assert str(row.report_id) == str(seeded["report_id"])
        assert str(row.trip_id) == str(seeded["trip_id"])
        assert str(row.smart_match_segment_id) == str(seeded["segment_id"])

    def test_smart_match_cross_link_resolves(
        self, seeded: dict[str, Any], db_engine: Engine
    ) -> None:
        """expenses.trip_segment_id → trip_segments FK resolves correctly."""
        factory = get_sessionmaker()
        with factory() as session:
            result = session.execute(
                sa.text("""
                    SELECT e.id AS expense_id, ts.id AS segment_id, ts.trip_id
                    FROM expenses e
                    JOIN trip_segments ts ON e.trip_segment_id = ts.id
                    WHERE e.id = :expense_id
                """),
                {"expense_id": str(seeded["expense_id"])},
            ).fetchone()

        assert result is not None
        assert str(result.expense_id) == str(seeded["expense_id"])
        assert str(result.segment_id) == str(seeded["segment_id"])
        assert str(result.trip_id) == str(seeded["trip_id"])

    def test_card_txn_grounding_resolves(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """card_transactions.expense_id → expenses FK resolves correctly."""
        factory = get_sessionmaker()
        with factory() as session:
            result = session.execute(
                sa.text("""
                    SELECT ct.id, ct.expense_id, e.id AS linked_expense_id
                    FROM card_transactions ct
                    JOIN expenses e ON ct.expense_id = e.id
                    WHERE ct.id = :ct_id
                """),
                {"ct_id": str(seeded["card_txn_id"])},
            ).fetchone()

        assert result is not None
        assert str(result.linked_expense_id) == str(seeded["expense_id"])


# ---------------------------------------------------------------------------
# TestOrphanIntegrity: global checks — zero dangling FK references
# ---------------------------------------------------------------------------


class TestOrphanIntegrity:
    def test_no_dangling_receipt_to_expense(self, db_engine: Engine) -> None:
        """No receipt has expense_id pointing to a nonexistent expense."""
        factory = get_sessionmaker()
        with factory() as session:
            count = session.execute(
                sa.text("""
                    SELECT COUNT(*) FROM receipts r
                    WHERE r.expense_id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM expenses e WHERE e.id = r.expense_id)
                """)
            ).scalar()
        assert count == 0, f"Found {count} receipts with dangling expense_id"

    def test_no_dangling_expense_to_report(self, db_engine: Engine) -> None:
        """No expense has report_id pointing to a nonexistent expense_report."""
        factory = get_sessionmaker()
        with factory() as session:
            count = session.execute(
                sa.text("""
                    SELECT COUNT(*) FROM expenses e
                    WHERE NOT EXISTS (
                        SELECT 1 FROM expense_reports er WHERE er.id = e.report_id
                    )
                """)
            ).scalar()
        assert count == 0, f"Found {count} expenses with dangling report_id"

    def test_no_dangling_expense_to_segment(self, db_engine: Engine) -> None:
        """No expense has trip_segment_id pointing to a nonexistent trip_segment."""
        factory = get_sessionmaker()
        with factory() as session:
            count = session.execute(
                sa.text("""
                    SELECT COUNT(*) FROM expenses e
                    WHERE e.trip_segment_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM trip_segments ts WHERE ts.id = e.trip_segment_id
                      )
                """)
            ).scalar()
        assert count == 0, f"Found {count} expenses with dangling trip_segment_id"

    def test_no_dangling_report_to_trip(self, db_engine: Engine) -> None:
        """No expense_report has trip_id pointing to a nonexistent trip."""
        factory = get_sessionmaker()
        with factory() as session:
            count = session.execute(
                sa.text("""
                    SELECT COUNT(*) FROM expense_reports er
                    WHERE er.trip_id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM trips t WHERE t.id = er.trip_id)
                """)
            ).scalar()
        assert count == 0, f"Found {count} expense_reports with dangling trip_id"

    def test_no_dangling_card_txn_to_expense(self, db_engine: Engine) -> None:
        """No card_transaction has expense_id pointing to a nonexistent expense."""
        factory = get_sessionmaker()
        with factory() as session:
            count = session.execute(
                sa.text("""
                    SELECT COUNT(*) FROM card_transactions ct
                    WHERE ct.expense_id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM expenses e WHERE e.id = ct.expense_id)
                """)
            ).scalar()
        assert count == 0, f"Found {count} card_transactions with dangling expense_id"

    def test_expected_nulls_documented(self, seeded: dict[str, Any], db_engine: Engine) -> None:
        """Document expected NULLs in fixture data — these are intentional, not broken links.

        Verifies the unmatched fixture records have the expected NULL FK values:
        - unmatched_receipt.expense_id = NULL (receipt not yet matched to an expense)
        - unmatched_report.trip_id = NULL (standalone report, not linked to a trip)
        - unmatched_expense.trip_segment_id = NULL (expense not smart-matched to a segment)
        - unmatched_card_txn.expense_id = NULL (card transaction not matched to an expense)
        """
        factory = get_sessionmaker()
        with factory() as session:
            unmatched_receipt = session.get(Receipt, seeded["unmatched_receipt_id"])
            unmatched_report = session.get(ExpenseReport, seeded["unmatched_report_id"])
            unmatched_expense = session.get(Expense, seeded["unmatched_expense_id"])
            unmatched_card_txn = session.get(CardTransaction, seeded["unmatched_card_txn_id"])

        assert unmatched_receipt is not None
        assert unmatched_receipt.expense_id is None, "Unmatched receipt should have null expense_id"

        assert unmatched_report is not None
        assert unmatched_report.trip_id is None, "Standalone report should have null trip_id"

        assert unmatched_expense is not None
        assert (
            unmatched_expense.trip_segment_id is None
        ), "Unmatched expense should have null trip_segment_id"

        assert unmatched_card_txn is not None
        assert (
            unmatched_card_txn.expense_id is None
        ), "Unmatched card txn should have null expense_id"
