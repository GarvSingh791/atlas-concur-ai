"""Add correlation_id to ai_decision_logs.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-23

Adds a nullable correlation_id column to ai_decision_logs so each AI
invocation can be tied back to the request that triggered it, plus a
non-unique index for lookups by correlation id.  Mirrors the existing
ix_ai_decision_logs_organization_id index style.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "ai_decision_logs",
        sa.Column("correlation_id", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_ai_decision_logs_correlation_id",
        "ai_decision_logs",
        ["correlation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_decision_logs_correlation_id", table_name="ai_decision_logs")
    op.drop_column("ai_decision_logs", "correlation_id")
