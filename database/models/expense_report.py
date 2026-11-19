"""ExpenseReport domain model.

Per DATABASE_SCHEMA.md §expense_reports.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id), authorship (created_by/updated_by),
  version (optimistic lock) — NO soft-delete (expense_reports field table omits deleted_at;
  same precedent as P2-T6 trips).

The owner_id FK (→ users) is the ownership column consumed by scope_query.
total_amount is DERIVED from entries and maintained atomically — never accepted from clients.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CHAR, ForeignKey, Index, Numeric, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import (
    AuthorshipMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    VersionMixin,
)


class ExpenseReportStatus(str, enum.Enum):
    """Legal lifecycle states for an expense report (PG type expense_report_status)."""

    draft = "draft"
    submitted = "submitted"
    approved = "approved"
    rejected = "rejected"
    reimbursed = "reimbursed"


class ExpenseReport(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TenantMixin,
    AuthorshipMixin,
    VersionMixin,
    Base,
):
    """An expense report.  owner_id is the ownership column for scope_query."""

    __tablename__ = "expense_reports"

    # Submitting employee (ownership column for scope_query)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Optional link to a trip — NULL until the AI smart-match runs (Phase 4)
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="SET NULL"),
        nullable=True,
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[ExpenseReportStatus] = mapped_column(
        SAEnum(ExpenseReportStatus, name="expense_report_status", create_type=True),
        nullable=False,
    )

    # Derived from entries — maintained atomically; never accepted from clients
    total_amount: Mapped[object] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        server_default=text("0"),
        default=0,
    )

    # ISO 4217 three-character code
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    # Set when the report transitions to submitted
    submitted_at: Mapped[object | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    # Snapshot of policy flags shown to the submitter before submit (Phase 4 populates this)
    pre_submit_flags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Product gap 1 (0016) — simulated reimbursement, set ONLY by the finance
    # reimburse action (approved -> reimbursed).  No real money moves.
    reimbursed_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reimbursed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    reimbursement_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    reimbursement_amount: Mapped[object | None] = mapped_column(Numeric(14, 2), nullable=True)

    # Product gap 4 (0016) — the owner withdrew a RETURNED report.  The status
    # stays ``draft``; a non-null withdrawn_at closes the report (read-only).
    withdrawn_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    withdrawn_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    withdrawal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_expense_reports_owner_id_status", "owner_id", "status"),
        Index("ix_expense_reports_organization_id", "organization_id"),
        Index("ix_expense_reports_trip_id", "trip_id"),
        Index(
            "uq_expense_reports_reimbursement_reference",
            "reimbursement_reference",
            unique=True,
        ),
    )

    entries: Mapped[list[object]] = relationship(
        "Expense",
        back_populates="report",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Expense.created_at",
    )
