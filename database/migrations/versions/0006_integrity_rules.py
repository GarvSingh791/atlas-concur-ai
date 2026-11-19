"""Integrity Rules — DB-level constraints missing from migrations 0001–0005.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-12

Three purely-additive changes (no existing constraints, tables, or models
are modified beyond these additions):

1. idempotency_keys — new support table for client-supplied idempotency keys.
   Polymorphic entity_id (no FK — same deliberate tradeoff as approvals.subject_id).
   Unique constraint: (organization_id, scope, idempotency_key).

2. uq_policies_active_per_scope — partial unique index on policies.
   Enforces: at most one active (is_active = true) policy per (organization_id, name).
   Does NOT conflict with uq_policies_organization_id_name_version which spans
   all versions regardless of is_active.

3. ck_trip_segments_end_datetime_gte_start_datetime — CHECK constraint on
   trip_segments.  NULL semantics: NULL >= anything is NULL, which does NOT
   violate the CHECK (PostgreSQL NULL comparison semantics).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Create idempotency_keys table
    # ------------------------------------------------------------------
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # CreatedAtMixin — append-only table, no updated_at
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # TenantMixin
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Scope (e.g. "expense_report.create")
        sa.Column("scope", sa.Text(), nullable=False),
        # Client-supplied idempotency key (opaque string)
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        # The PK of the entity created for this key — no FK (polymorphic)
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Constraints
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_idempotency_keys_organization_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "organization_id",
            "scope",
            "idempotency_key",
            name="uq_idempotency_keys_organization_id_scope_idempotency_key",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
    )

    # ------------------------------------------------------------------
    # 2. Partial unique index: one active policy per (organization_id, name)
    # ------------------------------------------------------------------
    op.create_index(
        "uq_policies_active_per_scope",
        "policies",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    # ------------------------------------------------------------------
    # 3. CHECK constraint: end_datetime >= start_datetime on trip_segments
    #    (NULLs pass — PostgreSQL NULL comparison returns NULL, not FALSE)
    # ------------------------------------------------------------------
    op.create_check_constraint(
        op.f("ck_trip_segments_end_datetime_gte_start_datetime"),
        "trip_segments",
        "end_datetime >= start_datetime",
    )


def downgrade() -> None:
    # Reverse in opposite order of creation.

    # 3. Drop CHECK constraint on trip_segments
    #    op.f() bypasses naming-convention expansion — same as upgrade path.
    op.drop_constraint(
        op.f("ck_trip_segments_end_datetime_gte_start_datetime"),
        "trip_segments",
        type_="check",
    )

    # 2. Drop partial unique index on policies
    op.drop_index("uq_policies_active_per_scope", table_name="policies")

    # 1. Drop idempotency_keys table
    op.drop_table("idempotency_keys")
