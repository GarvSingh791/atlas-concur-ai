"""Approval domain model.

Per DATABASE_SCHEMA.md §approvals.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id), version (optimistic lock).
  NO authorship — approvals track submitter_id explicitly; no created_by/updated_by.

Polymorphic subject: (subject_type, subject_id) references either a trip or
expense_report.  There is NO database FK on subject_id — referential integrity
is enforced at the application layer (documented deliberate tradeoff, keeps the
workflow engine generic without a junction table per subject type).
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import BOOLEAN, ForeignKey, Index
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin, VersionMixin


class ApprovalSubjectType(str, enum.Enum):
    """The kind of record being approved (PG type approval_subject_type)."""

    trip = "trip"
    expense_report = "expense_report"


class ApprovalStatus(str, enum.Enum):
    """Current workflow state of an approval (PG type approval_status)."""

    pending = "pending"
    auto_approved = "auto_approved"
    approved = "approved"
    rejected = "rejected"
    changes_requested = "changes_requested"


class Approval(
    UUIDPrimaryKeyMixin,
    TimestampMixin,
    TenantMixin,
    VersionMixin,
    Base,
):
    """Workflow instance for a submitted trip or expense report.

    subject_id has NO database FK — the polymorphic link is integrity-checked
    by the application layer.  This is a deliberate tradeoff documented in
    DATABASE_SCHEMA.md §Relationships (Polymorphic association).
    """

    __tablename__ = "approvals"

    subject_type: Mapped[ApprovalSubjectType] = mapped_column(
        SAEnum(ApprovalSubjectType, name="approval_subject_type", create_type=True),
        nullable=False,
    )

    # No FK constraint — polymorphic reference; app-layer integrity.
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )

    # The employee who submitted the trip/report for approval.
    submitter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    current_status: Mapped[ApprovalStatus] = mapped_column(
        SAEnum(ApprovalStatus, name="approval_status", create_type=True),
        nullable=False,
    )

    is_auto_approved: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        server_default=text("false"),
        default=False,
    )

    # Flags/exceptions snapshot that drove the decision.
    risk_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_approvals_organization_id_current_status", "organization_id", "current_status"),
        Index("ix_approvals_submitter_id", "submitter_id"),
        Index(
            "ix_approvals_org_pending",
            "organization_id",
            postgresql_where=text("current_status = 'pending'::approval_status"),
        ),
    )

    steps: Mapped[list[object]] = relationship(
        "ApprovalStep",
        back_populates="approval",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ApprovalStep.step_order",
    )
