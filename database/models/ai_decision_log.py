"""AiDecisionLog domain model.

Per DATABASE_SCHEMA.md §ai_decision_logs.  Conventions applied:
  UUID PK, created_at ONLY (append-only log), tenant (organization_id).
  No authorship (created_by/updated_by), no version, no soft-delete.

user_id FK: SET NULL — logs survive even if the actor user is deleted (audit).
confidence: NUMERIC(4,3) — stores 0.000–1.000 to three decimal places.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database.models.base import Base
from database.models.mixins import CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin


class AiAgent(str, enum.Enum):
    """Which AI agent produced this decision (PG type ai_agent)."""

    orchestrator = "orchestrator"
    travel = "travel"
    expense = "expense"
    policy = "policy"
    approval = "approval"
    receipt_analysis = "receipt_analysis"


class AiRoute(str, enum.Enum):
    """Routing pattern taken for this decision (PG type ai_route)."""

    deterministic = "deterministic"
    llm = "llm"


class ReflectionOutcome(str, enum.Enum):
    """Critic/reflection loop result (PG type reflection_outcome)."""

    passed = "passed"
    revised = "revised"
    rejected = "rejected"


class AiDecisionLog(UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin, Base):
    """First-class record of every AI invocation.

    Enables explainability, audit, and evaluation (FR-A5; Success Metrics).
    Doubles as the evaluation dataset for routing and accuracy metrics.
    created_at only — append-only log; rows are never updated or deleted.
    """

    __tablename__ = "ai_decision_logs"

    # Actor if any; NULL for system-initiated decisions.  SET NULL so logs
    # survive user deletion.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent: Mapped[AiAgent] = mapped_column(
        SAEnum(AiAgent, name="ai_agent", create_type=True),
        nullable=False,
    )

    # e.g., 'rank_options', 'gap_fill', 'policy_query'
    task_type: Mapped[str] = mapped_column(Text, nullable=False)

    route: Mapped[AiRoute] = mapped_column(
        SAEnum(AiRoute, name="ai_route", create_type=True),
        nullable=False,
    )

    # Abstracted provider id when LLM path used; NULL on deterministic path.
    model_provider: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Request-scoped correlation id, injected by the caller; NULL when absent.
    correlation_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Minimized inputs (PII-safe summary, not raw user data).
    input_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Tool calls + results.
    tools_used: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Decision result summary.
    output_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # 0.000–1.000 with three decimal places.
    confidence: Mapped[object | None] = mapped_column(Numeric(4, 3), nullable=True)

    # Critic/reflection loop result; NULL when reflection was not run.
    reflection_outcome: Mapped[ReflectionOutcome | None] = mapped_column(
        SAEnum(ReflectionOutcome, name="reflection_outcome", create_type=True),
        nullable=True,
    )

    # Performance signal in milliseconds.
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_ai_decision_logs_organization_id", "organization_id"),
        Index("ix_ai_decision_logs_user_id", "user_id"),
        Index("ix_ai_decision_logs_correlation_id", "correlation_id"),
    )
