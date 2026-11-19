"""Account recovery: one-time auth tokens, email verification and password-change time.

Revision ID: 0024_auth_tokens
Revises: 0023_entry_release_lineage
Create Date: 2026-11-15

Phase 12 follow-ups (accounts): password reset, invitations and email
verification.

  auth_tokens(id uuid pk, organization_id uuid fk organizations RESTRICT,
              user_id uuid fk users CASCADE, purpose text CHECK in
              ('password_reset','invite','email_verify'), token_hash text
              (SHA-256 hex, unique, CHECK ^[0-9a-f]{64}$), expires_at
              timestamptz, used_at timestamptz NULL, created_by_id uuid fk
              users SET NULL NULL, created_at timestamptz default now(),
              requested_ip_hash text NULL)
  index (user_id, purpose)

  users.email_verified_at    timestamptz NULL
  users.password_changed_at  timestamptz NULL

Safe on a populated database: ONE new, empty table plus two NULLABLE columns
with no default on ``users`` (a catalog-only ``ADD COLUMN`` on Postgres — no
table rewrite, no row touched).  Existing users keep NULL in both: "not
verified" (the UI shows a banner, nothing is blocked) and "password never
changed through the app" (their existing tokens stay valid).  No PG enum is
created or altered (``purpose`` is TEXT + CHECK).

``downgrade()`` drops exactly these: the table (with its constraints and index)
and the two columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0024_auth_tokens"
down_revision: str | None = "0023_entry_release_lineage"
branch_labels: str | None = None
depends_on: str | None = None

_USER_COLUMNS = ("email_verified_at", "password_changed_at")


def upgrade() -> None:
    for column in _USER_COLUMNS:
        op.add_column("users", sa.Column(column, sa.TIMESTAMP(timezone=True), nullable=True))

    op.create_table(
        "auth_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("requested_ip_hash", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('password_reset', 'invite', 'email_verify')",
            name=op.f("ck_auth_tokens_purpose_known"),
        ),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auth_tokens_token_hash_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_auth_tokens_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["users.id"],
            name=op.f("fk_auth_tokens_created_by_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_tokens")),
        sa.UniqueConstraint("token_hash", name="uq_auth_tokens_token_hash"),
    )
    op.create_index(
        "ix_auth_tokens_user_id_purpose", "auth_tokens", ["user_id", "purpose"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_user_id_purpose", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    for column in reversed(_USER_COLUMNS):
        op.drop_column("users", column)
