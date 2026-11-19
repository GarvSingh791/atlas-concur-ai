"""Indexing Strategy — delta over migrations 0001–0004.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-12

Realizes the full DATABASE_SCHEMA.md Indexing Strategy section as a delta
over what 0001–0004 already created.  This migration is INDEX-ONLY: no
table, column, or enum changes.

Indexes added (38 total):
  FK indexes (28): one per join-driving FK column not already covered by a
    leading unique constraint or composite index.  Authorship FKs
    (created_by/updated_by) are intentionally excluded — they are rarely
    query predicates and the doc's illustrative example list omits them.
  Named composites (5):
    ix_expense_reports_owner_id_status, ix_approvals_organization_id_current_status,
    ix_trips_traveler_id_status, ix_trips_start_date, ix_expenses_report_id_status.
  JSONB GIN (2): ix_receipts_extracted_fields, ix_policy_rules_definition.
  Partial (2): ix_approvals_org_pending, ix_receipts_org_ocr_pending.
  HNSW ANN (1): ix_policy_embeddings_embedding_hnsw (vector_cosine_ops).

Tenant-prefix rule applied: composites lead with organization_id only where no
more-selective tenant-bound column already leads (approvals queue).  For
expense_reports, trips, and expenses, the ownership FK leads because it already
implies exactly one organization.

ANN choice: HNSW over IVFFlat — requires no training rows, correct for an
empty/incrementally-populated table.  Default build params (m=16,
ef_construction=64) — no micro-tuning at this scale.  vector_cosine_ops
matches text-embedding-3-small (cosine-space embeddings).

Consolidations (no near-duplicates shipped):
  - users.organization_id covered by leading column of uq_users_organization_id_email.
  - policies.organization_id covered by leading column of
    uq_policies_organization_id_name_version.
  - expense_reports.owner_id, trips.traveler_id, expenses.report_id,
    approvals.organization_id are each the leading column of a named composite —
    no separate single-column FK index created.

Already existed before 0005 (not re-created):
  - ix_audit_log_entity_type_entity_id_created_at (created in 0001).
  - uq_users_organization_id_email, uq_roles_key, uq_permissions_key,
    uq_policies_organization_id_name_version (unique constraints from 0001/0004).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # FK indexes — users
    # ------------------------------------------------------------------
    op.create_index("ix_users_manager_id", "users", ["manager_id"])

    # ------------------------------------------------------------------
    # FK indexes — role_permissions
    # (role_id is the PK lead; permission_id needs its own index)
    # ------------------------------------------------------------------
    op.create_index(
        "ix_role_permissions_permission_id", "role_permissions", ["permission_id"]
    )

    # ------------------------------------------------------------------
    # FK indexes — audit_log
    # ------------------------------------------------------------------
    op.create_index("ix_audit_log_organization_id", "audit_log", ["organization_id"])
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])

    # ------------------------------------------------------------------
    # trips — composite indexes + FK indexes
    # (traveler_id leads the composite; organization_id needs its own index)
    # ------------------------------------------------------------------
    op.create_index(
        "ix_trips_traveler_id_status", "trips", ["traveler_id", "status"]
    )
    op.create_index("ix_trips_start_date", "trips", ["start_date"])
    op.create_index("ix_trips_organization_id", "trips", ["organization_id"])

    # ------------------------------------------------------------------
    # FK indexes — trip_segments
    # ------------------------------------------------------------------
    op.create_index("ix_trip_segments_trip_id", "trip_segments", ["trip_id"])

    # ------------------------------------------------------------------
    # expense_reports — composite index + FK indexes
    # (owner_id leads the composite; organization_id + trip_id need own indexes)
    # ------------------------------------------------------------------
    op.create_index(
        "ix_expense_reports_owner_id_status", "expense_reports", ["owner_id", "status"]
    )
    op.create_index(
        "ix_expense_reports_organization_id", "expense_reports", ["organization_id"]
    )
    op.create_index("ix_expense_reports_trip_id", "expense_reports", ["trip_id"])

    # ------------------------------------------------------------------
    # expenses — composite index + FK indexes
    # (report_id leads the composite; organization_id + trip_segment_id need own)
    # ------------------------------------------------------------------
    op.create_index(
        "ix_expenses_report_id_status", "expenses", ["report_id", "status"]
    )
    op.create_index("ix_expenses_organization_id", "expenses", ["organization_id"])
    op.create_index("ix_expenses_trip_segment_id", "expenses", ["trip_segment_id"])

    # ------------------------------------------------------------------
    # approvals — composite + partial + FK indexes
    # (organization_id leads both composite and partial; submitter_id needs own)
    # ------------------------------------------------------------------
    op.create_index(
        "ix_approvals_organization_id_current_status",
        "approvals",
        ["organization_id", "current_status"],
    )
    op.create_index("ix_approvals_submitter_id", "approvals", ["submitter_id"])
    op.create_index(
        "ix_approvals_org_pending",
        "approvals",
        ["organization_id"],
        postgresql_where=sa.text("current_status = 'pending'::approval_status"),
    )

    # ------------------------------------------------------------------
    # FK indexes — approval_steps
    # ------------------------------------------------------------------
    op.create_index(
        "ix_approval_steps_approval_id", "approval_steps", ["approval_id"]
    )
    op.create_index(
        "ix_approval_steps_approver_id", "approval_steps", ["approver_id"]
    )

    # ------------------------------------------------------------------
    # receipts — GIN + partial + FK indexes
    # ------------------------------------------------------------------
    op.create_index(
        "ix_receipts_extracted_fields",
        "receipts",
        ["extracted_fields"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_receipts_org_ocr_pending",
        "receipts",
        ["organization_id"],
        postgresql_where=sa.text("ocr_status = 'pending'::ocr_status"),
    )
    op.create_index(
        "ix_receipts_organization_id", "receipts", ["organization_id"]
    )
    op.create_index("ix_receipts_uploaded_by", "receipts", ["uploaded_by"])
    op.create_index("ix_receipts_expense_id", "receipts", ["expense_id"])

    # ------------------------------------------------------------------
    # policy_rules — GIN + FK index
    # ------------------------------------------------------------------
    op.create_index(
        "ix_policy_rules_definition",
        "policy_rules",
        ["definition"],
        postgresql_using="gin",
    )
    op.create_index("ix_policy_rules_policy_id", "policy_rules", ["policy_id"])

    # ------------------------------------------------------------------
    # policy_embeddings — FK index + HNSW ANN index
    # HNSW: no training rows needed (correct for empty/incremental table).
    # vector_cosine_ops: matches cosine-space OpenAI-class embeddings.
    # Default m=16, ef_construction=64 — sufficient at this scale.
    # ------------------------------------------------------------------
    op.create_index(
        "ix_policy_embeddings_policy_id", "policy_embeddings", ["policy_id"]
    )
    op.create_index(
        "ix_policy_embeddings_embedding_hnsw",
        "policy_embeddings",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # ------------------------------------------------------------------
    # FK indexes — card_transactions
    # ------------------------------------------------------------------
    op.create_index(
        "ix_card_transactions_organization_id",
        "card_transactions",
        ["organization_id"],
    )
    op.create_index(
        "ix_card_transactions_user_id", "card_transactions", ["user_id"]
    )
    op.create_index(
        "ix_card_transactions_expense_id", "card_transactions", ["expense_id"]
    )

    # ------------------------------------------------------------------
    # FK indexes — calendar_events
    # ------------------------------------------------------------------
    op.create_index(
        "ix_calendar_events_organization_id",
        "calendar_events",
        ["organization_id"],
    )
    op.create_index(
        "ix_calendar_events_user_id", "calendar_events", ["user_id"]
    )

    # ------------------------------------------------------------------
    # FK indexes — conversations
    # ------------------------------------------------------------------
    op.create_index(
        "ix_conversations_organization_id", "conversations", ["organization_id"]
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    # ------------------------------------------------------------------
    # FK indexes — conversation_messages
    # ------------------------------------------------------------------
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )

    # ------------------------------------------------------------------
    # FK indexes — ai_decision_logs
    # ------------------------------------------------------------------
    op.create_index(
        "ix_ai_decision_logs_organization_id",
        "ai_decision_logs",
        ["organization_id"],
    )
    op.create_index(
        "ix_ai_decision_logs_user_id", "ai_decision_logs", ["user_id"]
    )


def downgrade() -> None:
    # Drop in reverse creation order.
    op.drop_index("ix_ai_decision_logs_user_id", table_name="ai_decision_logs")
    op.drop_index(
        "ix_ai_decision_logs_organization_id", table_name="ai_decision_logs"
    )
    op.drop_index(
        "ix_conversation_messages_conversation_id",
        table_name="conversation_messages",
    )
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_index(
        "ix_conversations_organization_id", table_name="conversations"
    )
    op.drop_index("ix_calendar_events_user_id", table_name="calendar_events")
    op.drop_index(
        "ix_calendar_events_organization_id", table_name="calendar_events"
    )
    op.drop_index(
        "ix_card_transactions_expense_id", table_name="card_transactions"
    )
    op.drop_index("ix_card_transactions_user_id", table_name="card_transactions")
    op.drop_index(
        "ix_card_transactions_organization_id", table_name="card_transactions"
    )
    op.drop_index(
        "ix_policy_embeddings_embedding_hnsw",
        table_name="policy_embeddings",
        postgresql_using="hnsw",
    )
    op.drop_index(
        "ix_policy_embeddings_policy_id", table_name="policy_embeddings"
    )
    op.drop_index(
        "ix_policy_rules_policy_id", table_name="policy_rules"
    )
    op.drop_index(
        "ix_policy_rules_definition",
        table_name="policy_rules",
        postgresql_using="gin",
    )
    op.drop_index("ix_receipts_expense_id", table_name="receipts")
    op.drop_index("ix_receipts_uploaded_by", table_name="receipts")
    op.drop_index("ix_receipts_organization_id", table_name="receipts")
    op.drop_index("ix_receipts_org_ocr_pending", table_name="receipts")
    op.drop_index(
        "ix_receipts_extracted_fields",
        table_name="receipts",
        postgresql_using="gin",
    )
    op.drop_index(
        "ix_approval_steps_approver_id", table_name="approval_steps"
    )
    op.drop_index(
        "ix_approval_steps_approval_id", table_name="approval_steps"
    )
    op.drop_index("ix_approvals_org_pending", table_name="approvals")
    op.drop_index("ix_approvals_submitter_id", table_name="approvals")
    op.drop_index(
        "ix_approvals_organization_id_current_status", table_name="approvals"
    )
    op.drop_index("ix_expenses_trip_segment_id", table_name="expenses")
    op.drop_index("ix_expenses_organization_id", table_name="expenses")
    op.drop_index("ix_expenses_report_id_status", table_name="expenses")
    op.drop_index("ix_expense_reports_trip_id", table_name="expense_reports")
    op.drop_index(
        "ix_expense_reports_organization_id", table_name="expense_reports"
    )
    op.drop_index(
        "ix_expense_reports_owner_id_status", table_name="expense_reports"
    )
    op.drop_index("ix_trip_segments_trip_id", table_name="trip_segments")
    op.drop_index("ix_trips_organization_id", table_name="trips")
    op.drop_index("ix_trips_start_date", table_name="trips")
    op.drop_index("ix_trips_traveler_id_status", table_name="trips")
    op.drop_index("ix_audit_log_actor_id", table_name="audit_log")
    op.drop_index("ix_audit_log_organization_id", table_name="audit_log")
    op.drop_index(
        "ix_role_permissions_permission_id", table_name="role_permissions"
    )
    op.drop_index("ix_users_manager_id", table_name="users")
