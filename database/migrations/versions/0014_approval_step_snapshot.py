"""Freeze what a human reviewer returned: approval_steps.subject_snapshot.

Revision ID: 0014_approval_step_snapshot
Revises: 0013_expense_justification
Create Date: 2026-11-11

Additive only.  Adds one nullable ``JSONB`` column ``subject_snapshot`` to
``approval_steps``.  When a human REJECTS or REQUESTS CHANGES on a subject, the
step records the subject's claim content at that moment (expense report: each
entry's amount / currency / date; trip: destination + dates).  The Approval
Agent's re-file check matches new submissions against these frozen rows, so
editing a returned subject in place cannot launder its original content into an
auto-approval (C7R segregation of duties).  NULL on every existing row and on
approve / auto_approve steps.  ``downgrade()`` drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0014_approval_step_snapshot"
down_revision: str | None = "0013_expense_justification"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "approval_steps",
        sa.Column("subject_snapshot", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("approval_steps", "subject_snapshot")
