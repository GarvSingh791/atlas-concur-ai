"""Add the claimant's policy-exception justification to expense entries.

Revision ID: 0013_expense_justification
Revises: 0012_hnsw_ef_construction_200
Create Date: 2026-11-11

Additive only.  Adds one nullable ``Text`` column ``justification`` to
``expenses``.  A non-empty justification makes a justifiable policy violation
(amount_cap, per_diem, preferred_supplier) advisory instead of
blocking in the pre-submit check; the flag stays visible to the approver.  NULL
on every existing row (no backfill).  ``downgrade()`` drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0013_expense_justification"
down_revision: str | None = "0012_hnsw_ef_construction_200"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("expenses", sa.Column("justification", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("expenses", "justification")
