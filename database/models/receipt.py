"""Receipt domain model.

Per DATABASE_SCHEMA.md §receipts.  Conventions applied:
  UUID PK, timestamps, tenant (organization_id).
  `uploaded_by` is an EXPLICIT FK column (NOT the AuthorshipMixin which adds
  created_by/updated_by pairs).  No version, no soft-delete.

uploaded_by FK: RESTRICT — receipts must survive the uploader for audit.
expense_id FK: SET NULL — link cleared if the expense is deleted.

Four JSONB columns capture OCR output and AI provenance per field.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text

from database.models.base import Base
from database.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OcrStatus(str, enum.Enum):
    """OCR pipeline state (PG type ocr_status)."""

    pending = "pending"
    processed = "processed"
    failed = "failed"


class ProcessingPath(str, enum.Enum):
    """Which processing path resolved the receipt (PG type processing_path)."""

    deterministic = "deterministic"
    agent_gap_fill = "agent_gap_fill"


class Receipt(UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, Base):
    """A captured receipt image with extracted fields and AI provenance.

    uploaded_by is an explicit FK column, not the AuthorshipMixin pair
    (created_by/updated_by), because receipts have a single uploader actor
    and no separate update-author concept.
    """

    __tablename__ = "receipts"

    # The user who uploaded the receipt image.
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Linked once matched to an expense entry; NULL until then.
    expense_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("expenses.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Reference to object storage (not the binary; S3-compatible via MinIO).
    image_url: Mapped[str] = mapped_column(Text, nullable=False)

    ocr_status: Mapped[OcrStatus] = mapped_column(
        SAEnum(OcrStatus, name="ocr_status", create_type=True),
        nullable=False,
    )

    # NULL until OCR processing completes.
    processing_path: Mapped[ProcessingPath | None] = mapped_column(
        SAEnum(ProcessingPath, name="processing_path", create_type=True),
        nullable=True,
    )

    # { amount, currency, date, vendor, location, expense_type, ... }
    extracted_fields: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Per-field confidence scores (0.0–1.0).
    field_confidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Per-field: 'deterministic' | 'ai_inferred' | 'user_edited'.
    field_provenance: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Trip/calendar context the agent used during gap-fill (audit lineage).
    grounding_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_receipts_extracted_fields", "extracted_fields", postgresql_using="gin"),
        Index(
            "ix_receipts_org_ocr_pending",
            "organization_id",
            postgresql_where=text("ocr_status = 'pending'::ocr_status"),
        ),
        Index("ix_receipts_organization_id", "organization_id"),
        Index("ix_receipts_uploaded_by", "uploaded_by"),
        Index("ix_receipts_expense_id", "expense_id"),
    )
