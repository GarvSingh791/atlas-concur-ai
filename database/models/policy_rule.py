"""PolicyRule domain model.

Per DATABASE_SCHEMA.md §policy_rules.  Conventions applied:
  UUID PK, timestamps, authorship (created_by/updated_by).
  NO TenantMixin — scoped via policy_id FK (ON DELETE CASCADE);
  organization context is inherited from the parent policy.

Enums: rule_type, applies_to, policy_rule_status.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.models.base import Base
from database.models.mixins import AuthorshipMixin, TimestampMixin, UUIDPrimaryKeyMixin


class PolicyRuleType(str, enum.Enum):
    """What the rule constrains (PG type policy_rule_type)."""

    amount_cap = "amount_cap"
    category_allow = "category_allow"
    cabin_class = "cabin_class"
    per_diem = "per_diem"
    preferred_supplier = "preferred_supplier"
    receipt_required = "receipt_required"
    # Product gap 2 (migration 0019) — evaluated by ai_agents/tools/spend_policy.py.
    per_head_cap = "per_head_cap"  # MEAL-05 team meals, ENT-02 client entertainment
    gift_disclosure = "gift_disclosure"  # ENT-04
    mileage = "mileage"  # MIL-01 rate, MIL-02 commute offset, MIL-03 air-equivalent cap


class PolicyAppliesTo(str, enum.Enum):
    """Scope of the rule (PG type policy_applies_to)."""

    flight = "flight"
    hotel = "hotel"
    rail = "rail"
    car = "car"
    meals = "meals"
    all = "all"


class PolicyRuleStatus(str, enum.Enum):
    """Authoring lifecycle for a rule (PG type policy_rule_status)."""

    draft = "draft"
    active = "active"
    archived = "archived"


class PolicyRule(UUIDPrimaryKeyMixin, TimestampMixin, AuthorshipMixin, Base):
    """A structured, testable rule derived from a policy.

    No organization_id — scoped via policy_id FK (inherits tenant from Policy).
    CASCADE: rules are hard-deleted when the parent policy is deleted.
    """

    __tablename__ = "policy_rules"

    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("policies.id", ondelete="CASCADE"),
        nullable=False,
    )

    rule_type: Mapped[PolicyRuleType] = mapped_column(
        SAEnum(PolicyRuleType, name="policy_rule_type", create_type=True),
        nullable=False,
    )

    applies_to: Mapped[PolicyAppliesTo] = mapped_column(
        SAEnum(PolicyAppliesTo, name="policy_applies_to", create_type=True),
        nullable=False,
    )

    # Machine-evaluable rule body (thresholds, allowed values, etc.)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Citation back into the policy document; NULL on programmatically generated rules.
    source_clause_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[PolicyRuleStatus] = mapped_column(
        SAEnum(PolicyRuleStatus, name="policy_rule_status", create_type=True),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_policy_rules_definition", "definition", postgresql_using="gin"),
        Index("ix_policy_rules_policy_id", "policy_id"),
    )

    policy: Mapped[object] = relationship("Policy", back_populates="rules")
