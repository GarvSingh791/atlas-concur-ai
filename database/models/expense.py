"""Expense (entry) domain model — child of ExpenseReport.

Per DATABASE_SCHEMA.md §expenses.  Convention columns: UUID PK + timestamps +
tenant (organization_id) + authorship (created_by/updated_by) ONLY.
NO version (no optimistic lock on entries), NO deleted_at (hard-deleted via
CASCADE when parent report is deleted).

report_id FK carries ON DELETE CASCADE so entries are true children of reports.
trip_segment_id FK carries ON DELETE SET NULL — the AI smart-match link (Phase 4).
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    BOOLEAN,
    CHAR,
    TIMESTAMP,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import (
    AuthorshipMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ExpenseCategory(str, enum.Enum):
    """Expense category (PG type expense_category)."""

    airfare = "airfare"
    lodging = "lodging"
    meals = "meals"
    ground_transport = "ground_transport"
    other = "other"
    # Product gap 2 (migration 0019): manual-only categories.  ``mileage`` amounts
    # are computed server-side (MIL-01); ``entertainment`` / ``gift`` carry the
    # ENT-02 / ENT-04 fields.  Never produced by receipt categorisation.
    mileage = "mileage"
    entertainment = "entertainment"
    gift = "gift"


class ExpenseStatus(str, enum.Enum):
    """Entry-level state (PG type expense_status)."""

    draft = "draft"
    matched = "matched"
    flagged = "flagged"
    approved = "approved"


class ExpenseMatchSource(str, enum.Enum):
    """Provenance of the entry (PG type expense_match_source)."""

    receipt = "receipt"
    card_txn = "card_txn"
    manual = "manual"
    trip = "trip"


class Expense(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TenantMixin,
    AuthorshipMixin,
    Base,
):
    """One expense line item within a report.

    No version (no optimistic lock), no deleted_at (cascade-deleted with report).
    """

    __tablename__ = "expenses"

    # Parent report — CASCADE: entries are hard-deleted when the report is deleted
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expense_reports.id", ondelete="CASCADE"),
        nullable=False,
    )

    # AI smart-match link to a trip segment (Phase 4); NULL until then
    trip_segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trip_segments.id", ondelete="SET NULL"),
        nullable=True,
    )

    category: Mapped[ExpenseCategory] = mapped_column(
        SAEnum(ExpenseCategory, name="expense_category", create_type=True),
        nullable=False,
    )

    amount: Mapped[object] = mapped_column(Numeric(14, 2), nullable=False)

    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    expense_date: Mapped[object] = mapped_column(Date, nullable=False)

    vendor: Mapped[str | None] = mapped_column(Text, nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The claimant's written justification for a policy exception on this entry
    # (C7R UAT).  A non-empty justification turns a justifiable rule violation
    # (amount cap, per diem, preferred supplier, cabin class) from a blocking
    # pre-submit flag into an advisory one the approver sees; it never clears
    # hard blocks (missing receipt, disallowed category, possible duplicate).
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)

    # EXP-04 (product gap 2, migration 0018): when the owner signed the missing-receipt
    # affidavit for this entry (NULL = not signed).  Server-set from PATCH
    # ``receipt_affidavit: true``; the justification carries the explanation.
    receipt_affidavit_at: Mapped[object | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    status: Mapped[ExpenseStatus] = mapped_column(
        SAEnum(ExpenseStatus, name="expense_status", create_type=True),
        nullable=False,
    )

    # NULL on hand-entered entries; set by AI receipt/card matching (Phase 4)
    match_source: Mapped[ExpenseMatchSource | None] = mapped_column(
        SAEnum(ExpenseMatchSource, name="expense_match_source", create_type=True),
        nullable=True,
    )

    is_itemized: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        server_default=text("false"),
        default=False,
    )

    # Product gap 2 (migration 0019) — policy fields captured on the entry.
    # Number of people at a meal / client entertainment INCLUDING the claimant
    # (MEAL-05 / ENT-02 per-head caps).  NULL = not recorded.
    attendee_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ENT-04 gift / hospitality disclosure: {recipient, recipient_organization,
    # business_purpose, government_official}.  NULL = none given.
    disclosure: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # MIL-01..03 personal-vehicle trip (category ``mileage`` only): what the
    # claimant entered + what the server derived ({distance, unit, origin,
    # destination, round_trip, commute_offset, air_impractical, one_way_miles,
    # billable_miles, rate_per_mile, rate_clause}).  The entry amount is
    # billable_miles x rate_per_mile, computed server-side — never client-set.
    mileage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Phase 10 (migration 0023) — re-file lineage.  Set by move_entries, in the
    # move's own transaction, when the line leaves a report its owner WITHDREW
    # after a reviewer returned / rejected it; never cleared and never client-set
    # (editing the line keeps it).  NULL = never released (or moved before 0023).
    # FK-less on purpose: an audit reference that must outlive its target.
    released_from_report_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    released_from_approval_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    released_at: Mapped[object | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint(
            "attendee_count IS NULL OR attendee_count >= 1", name="attendee_count_positive"
        ),
        Index("ix_expenses_report_id_status", "report_id", "status"),
        Index("ix_expenses_organization_id", "organization_id"),
        Index("ix_expenses_trip_segment_id", "trip_segment_id"),
    )

    report: Mapped[object] = relationship("ExpenseReport", back_populates="entries")
