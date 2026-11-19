"""Precise re-file lineage on expense lines: where a released line came from.

Revision ID: 0023_entry_release_lineage
Revises: 0022_org_scoped_email
Create Date: 2026-11-14

Known limit "over-flagging on re-filing" (phase 10).  When the owner moves a line
out of a report they WITHDREW after a reviewer returned or rejected it, the move
now records the line's origin on the line itself, in the same transaction:

  expenses.released_from_report_id    uuid NULL  the withdrawn report it left
  expenses.released_from_approval_id  uuid NULL  that report's approval (the
                                                 reviewer's return / reject lives
                                                 on its steps)
  expenses.released_at                timestamptz NULL  when it was moved out

The approval agent's re-file check then flags exactly those lines instead of
matching every new line on amount / date / currency.

No foreign keys on purpose: the lineage is an audit reference that must outlive
anything it points at (a report with approval history is never deleted anyway),
and an FK would add a validation scan of ``expenses`` on a populated database.

Additive only: three NULLABLE columns with no default, so ``ADD COLUMN`` is a
catalog-only change on Postgres (no table rewrite, no row touched).  Existing
lines keep NULL lineage and the agent keeps today's matching for them (legacy,
fail safe).  ``downgrade()`` drops exactly these three columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0023_entry_release_lineage"
down_revision: str | None = "0022_org_scoped_email"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS = ("released_from_report_id", "released_from_approval_id", "released_at")


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column("released_from_report_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("released_from_approval_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("released_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column("expenses", column)
