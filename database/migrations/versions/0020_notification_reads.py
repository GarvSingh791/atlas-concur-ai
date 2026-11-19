"""Per-user read state for derived notifications: notification_reads; and one
account per email across all organizations: uq_users_email_lower.

Revision ID: 0020_notification_reads
Revises: 0019_spend_caps_mileage
Create Date: 2026-11-14

Additive only (product gap 3, app-shell notifications).  Creates ONE new, empty
table — no existing table is altered or rewritten, so it is safe on a populated
database.  Notifications themselves are derived on read from existing workflow
state; this table only records which derived keys a user has marked read.

  notification_reads(id uuid pk, user_id uuid fk users ON DELETE CASCADE,
                     notification_key text 1..200 chars, read_at timestamptz)
  unique (user_id, notification_key) · index (user_id, read_at)

It also adds ONE unique expression index, ``uq_users_email_lower`` on
``users (lower(email))``.  Login resolves a user by email alone (no org), so the
same email in two organizations made the login target depend on physical row
order — one tenant's admin could lock out another tenant's user (review finding
on POST /users).  Before creating it the migration counts case-insensitive
duplicate emails; if any exist it FAILS CLOSED with an operator message (counts
only, no emails — no PII in logs) instead of guessing which account to keep.
No row is modified.  ``users`` is small, so a plain (non-concurrent) CREATE
INDEX inside the migration transaction is a brief lock, not a rewrite.

``downgrade()`` drops the index, then the table (and with it, its constraints
and index).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0020_notification_reads"
# Integration (product gaps): one linear history 0015 -> 0016 -> 0017 -> 0018
# -> 0019 -> 0020 (only this line and the docstring changed).
down_revision: str | None = "0019_spend_caps_mileage"
branch_labels: str | None = None
depends_on: str | None = None


_EMAIL_INDEX = "uq_users_email_lower"


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT count(*) FROM (SELECT lower(email) FROM users "
            "GROUP BY lower(email) HAVING count(*) > 1) AS d"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"0020: {duplicates} email address(es) are shared by more than one user "
            "(case-insensitive). Login resolves users by email across all "
            "organizations, so each email must belong to exactly one account. "
            "Resolve the duplicates manually, then re-run the migration."
        )
    op.create_index(_EMAIL_INDEX, "users", [sa.text("lower(email)")], unique=True)

    op.create_table(
        "notification_reads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notification_key", sa.Text(), nullable=False),
        sa.Column(
            "read_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(notification_key) BETWEEN 1 AND 200",
            name=op.f("ck_notification_reads_notification_key_length"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_notification_reads_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_reads")),
        sa.UniqueConstraint(
            "user_id",
            "notification_key",
            name="uq_notification_reads_user_id_notification_key",
        ),
    )
    op.create_index(
        "ix_notification_reads_user_id_read_at",
        "notification_reads",
        ["user_id", "read_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(_EMAIL_INDEX, table_name="users")
    op.drop_index("ix_notification_reads_user_id_read_at", table_name="notification_reads")
    op.drop_table("notification_reads")
