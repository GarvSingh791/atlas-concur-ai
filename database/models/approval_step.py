"""ApprovalStep domain model.

Per DATABASE_SCHEMA.md §approval_steps.  Conventions applied:
  UUID PK, created_at ONLY (append-only decision record).
  No tenant (scoped via approval_id), no authorship, no timestamps/version/soft-delete.

approval_id FK: CASCADE — steps are deleted when the parent approval is deleted.
approver_id FK: SET NULL — NULL indicates a system auto-approve action.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Integer, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import CreatedAtMixin, UUIDPrimaryKeyMixin


class ApprovalDecision(str, enum.Enum):
    """The decision made at this step (PG type approval_decision)."""

    approve = "approve"
    reject = "reject"
    request_changes = "request_changes"
    auto_approve = "auto_approve"


class ApprovalStep(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One decision action on an approval.

    Supports single-level now; schema supports multi-step later via step_order.
    approver_id is NULL when the system auto-approved (no human actor).
    created_at only — this is an append-only audit record of decisions.
    """

    __tablename__ = "approval_steps"

    # Parent approval — CASCADE: steps are hard-deleted when approval is deleted.
    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("approvals.id", ondelete="CASCADE"),
        nullable=False,
    )

    # NULL when system auto-approved (no human actor).
    approver_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    step_order: Mapped[int] = mapped_column(Integer, nullable=False)

    decision: Mapped[ApprovalDecision] = mapped_column(
        SAEnum(ApprovalDecision, name="approval_decision", create_type=True),
        nullable=False,
    )

    # Required for reject/request_changes (application-enforced, P3-T4).
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    decided_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    # C7R SoD: on a human reject / request_changes, the subject's claim content at that
    # moment (report: [{amount, currency, expense_date}], trip: {destination, start_date,
    # end_date}) so a later re-file of the ORIGINAL content is caught even if the returned
    # subject was edited in place afterwards.  NULL otherwise.
    subject_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_approval_steps_approval_id", "approval_id"),
        Index("ix_approval_steps_approver_id", "approver_id"),
    )

    approval: Mapped[object] = relationship("Approval", back_populates="steps")
