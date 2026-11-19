"""CardTransaction domain model (simulated corporate card swipes).

Per DATABASE_SCHEMA.md §card_transactions.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id).
  NO authorship (system-captured), NO version, NO soft-delete.

user_id FK carries RESTRICT — transactions must survive the user for audit.
expense_id FK carries SET NULL — link cleared when the expense is deleted.
"""

from __future__ import annotations

import uuid

from sqlalchemy import BOOLEAN, CHAR, Date, ForeignKey, Index, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class CardTransaction(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A simulated corporate card swipe.

    Provides a second capture source for smart matching (card-swipe-to-expense).
    No authorship or version — system-captured data.
    """

    __tablename__ = "card_transactions"

    # The cardholder (RESTRICT: transactions survive user for audit).
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Linked once matched to an expense entry; NULL until then.
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expenses.id", ondelete="SET NULL"),
        nullable=True,
    )

    amount: Mapped[object] = mapped_column(Numeric(14, 2), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)

    merchant: Mapped[str | None] = mapped_column(Text, nullable=True)

    txn_date: Mapped[object] = mapped_column(Date, nullable=False)

    # Used for corporate-card detection; NULL for general purchases.
    last_four: Mapped[str | None] = mapped_column(CHAR(4), nullable=True)

    matched: Mapped[bool] = mapped_column(
        BOOLEAN,
        nullable=False,
        server_default=text("false"),
        default=False,
    )

    __table_args__ = (
        Index("ix_card_transactions_organization_id", "organization_id"),
        Index("ix_card_transactions_user_id", "user_id"),
        Index("ix_card_transactions_expense_id", "expense_id"),
    )
