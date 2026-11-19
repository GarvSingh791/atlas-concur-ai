"""Report & trip lifecycle: simulated reimbursement, owner withdrawal (product gaps 1/4/8).

Revision ID: 0016_report_trip_lifecycle
Revises: 0015_canonical_policy_figures
Create Date: 2026-11-14

Schema (additive only, every column NULLABLE — no table rewrite, no backfill):

  expense_reports
    reimbursed_at            timestamptz   set when finance marks the report reimbursed
    reimbursed_by_id         uuid FK users (RESTRICT) — the finance user who did it
    reimbursement_reference  text          server-generated simulated payment reference
                                           (unique when present; NULLs are distinct)
    reimbursement_amount     numeric(14,2) the report total reimbursed, in its currency
    withdrawn_at             timestamptz   set when the owner withdraws a RETURNED report
    withdrawn_by_id          uuid FK users (RESTRICT)
    withdrawal_reason        text          optional owner note
  trips
    withdrawn_at / withdrawn_by_id / withdrawal_reason  (same meaning, returned trip)

A withdrawn report keeps status ``draft`` (there is no "returned" status either: a
returned report is a draft whose approval is changes_requested / rejected); the
non-null ``withdrawn_at`` closes it.  A new PG enum value was avoided on purpose
(ALTER TYPE ... ADD VALUE cannot be used in the same transaction and has no
symmetric downgrade).

Data (idempotent, add-only): the permission ``expense.reimburse.org`` and its
grant to the ``finance_admin`` role — so a live database that does not re-run the
identity seed (staging boots with SEED_ON_STARTUP=policy_corpus) gets the finance
reimburse action.  Inserted with ON CONFLICT DO NOTHING; on a fresh database the
roles table is still empty here, so only the permission row is inserted and the
seed grants it afterwards.

downgrade(): drops the grant + permission rows (only the row with this exact key)
and every added column / index / FK, in reverse order.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0016_report_trip_lifecycle"
down_revision: str | None = "0015_canonical_policy_figures"
branch_labels: str | None = None
depends_on: str | None = None

# FROZEN copies — never import from config.constants (it may change later).
REIMBURSE_PERMISSION_KEY = "expense.reimburse.org"
REIMBURSE_PERMISSION_DESCRIPTION = (
    "Mark approved expense reports reimbursed (simulated payment, finance only)"
)
FINANCE_ROLE_KEY = "finance_admin"


def upgrade() -> None:
    op.add_column(
        "expense_reports",
        sa.Column("reimbursed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "expense_reports",
        sa.Column("reimbursed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("expense_reports", sa.Column("reimbursement_reference", sa.Text(), nullable=True))
    op.add_column(
        "expense_reports",
        sa.Column("reimbursement_amount", sa.Numeric(14, 2), nullable=True),
    )
    op.add_column(
        "expense_reports",
        sa.Column("withdrawn_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "expense_reports",
        sa.Column("withdrawn_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("expense_reports", sa.Column("withdrawal_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_expense_reports_reimbursed_by_id_users"),
        "expense_reports",
        "users",
        ["reimbursed_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        op.f("fk_expense_reports_withdrawn_by_id_users"),
        "expense_reports",
        "users",
        ["withdrawn_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_expense_reports_reimbursement_reference",
        "expense_reports",
        ["reimbursement_reference"],
        unique=True,
    )

    op.add_column(
        "trips", sa.Column("withdrawn_at", postgresql.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "trips", sa.Column("withdrawn_by_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("trips", sa.Column("withdrawal_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_trips_withdrawn_by_id_users"),
        "trips",
        "users",
        ["withdrawn_by_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO permissions (id, key, description) "
            "VALUES (gen_random_uuid(), :key, :description) "
            "ON CONFLICT (key) DO NOTHING"
        ),
        {"key": REIMBURSE_PERMISSION_KEY, "description": REIMBURSE_PERMISSION_DESCRIPTION},
    )
    bind.execute(
        sa.text(
            "INSERT INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.key = :role AND p.key = :key "
            "ON CONFLICT (role_id, permission_id) DO NOTHING"
        ),
        {"role": FINANCE_ROLE_KEY, "key": REIMBURSE_PERMISSION_KEY},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN "
            "(SELECT id FROM permissions WHERE key = :key)"
        ),
        {"key": REIMBURSE_PERMISSION_KEY},
    )
    bind.execute(
        sa.text("DELETE FROM permissions WHERE key = :key"), {"key": REIMBURSE_PERMISSION_KEY}
    )

    op.drop_constraint(op.f("fk_trips_withdrawn_by_id_users"), "trips", type_="foreignkey")
    op.drop_column("trips", "withdrawal_reason")
    op.drop_column("trips", "withdrawn_by_id")
    op.drop_column("trips", "withdrawn_at")

    op.drop_index("uq_expense_reports_reimbursement_reference", table_name="expense_reports")
    op.drop_constraint(
        op.f("fk_expense_reports_withdrawn_by_id_users"), "expense_reports", type_="foreignkey"
    )
    op.drop_constraint(
        op.f("fk_expense_reports_reimbursed_by_id_users"), "expense_reports", type_="foreignkey"
    )
    op.drop_column("expense_reports", "withdrawal_reason")
    op.drop_column("expense_reports", "withdrawn_by_id")
    op.drop_column("expense_reports", "withdrawn_at")
    op.drop_column("expense_reports", "reimbursement_amount")
    op.drop_column("expense_reports", "reimbursement_reference")
    op.drop_column("expense_reports", "reimbursed_by_id")
    op.drop_column("expense_reports", "reimbursed_at")
