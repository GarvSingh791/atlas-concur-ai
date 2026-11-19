"""Receipt storage quotas — per user and per organisation (phase 11).

Counts every receipt row the user (or the organisation) still has, and the
bytes stored for them.  The receipts table has no size column (this track adds
no migration), so a receipt's stored size is read from the ``stored_bytes``
field of its ``receipt.processed`` audit row, which the upload writes in the
same transaction as the row itself.  A deleted receipt drops out of the join,
so deleting frees its share at once.  Receipts stored before phase 11 (and
seeded ones) have no such field: they count as files but as 0 bytes.

Fail closed: the check runs before anything is stored; a breach raises 409
``UPLOAD_QUOTA_EXCEEDED`` and nothing is written.  It is not serialised across
concurrent requests, so simultaneous uploads can overshoot by at most the
number in flight (documented in the phase log).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.modules.receipt.upload_guard import UploadQuotaExceededError
from config.settings import get_settings
from database.models.audit_log import AuditLog
from database.models.identity import User
from database.models.receipt import Receipt

PROCESSED_ACTION = "receipt.processed"
STORED_BYTES_KEY = "stored_bytes"

_MIB = 1024 * 1024


@dataclass(frozen=True)
class Usage:
    files: int
    bytes: int


def _stored_bytes() -> sa.ColumnElement[int]:
    value = AuditLog.after_state[STORED_BYTES_KEY]
    return sa.case(
        (
            sa.func.jsonb_typeof(value) == "number",
            AuditLog.after_state[STORED_BYTES_KEY].astext.cast(sa.BigInteger),
        ),
        else_=0,
    )


def receipt_usage(
    db: Session, *, organization_id: uuid.UUID, uploaded_by: uuid.UUID | None = None
) -> Usage:
    """Files and bytes held by *uploaded_by* (or the whole organisation when None)."""
    stmt = (
        sa.select(
            sa.func.count(sa.distinct(Receipt.id)),
            sa.func.coalesce(sa.func.sum(_stored_bytes()), 0),
        )
        .select_from(Receipt)
        .outerjoin(
            AuditLog,
            sa.and_(
                AuditLog.organization_id == Receipt.organization_id,
                AuditLog.entity_type == "receipt",
                AuditLog.entity_id == Receipt.id,
                AuditLog.action == PROCESSED_ACTION,
            ),
        )
        .where(Receipt.organization_id == organization_id)
    )
    if uploaded_by is not None:
        stmt = stmt.where(Receipt.uploaded_by == uploaded_by)
    files, total = db.execute(stmt).one()
    return Usage(files=int(files or 0), bytes=int(total or 0))


def _mb(n: int) -> str:
    return f"{n / _MIB:.0f} MB"


def check_upload_quota(db: Session, user: User, incoming_bytes: int) -> None:
    """Raise 409 UPLOAD_QUOTA_EXCEEDED when one more file of *incoming_bytes* would not fit."""
    settings = get_settings()
    mine = receipt_usage(db, organization_id=user.organization_id, uploaded_by=user.id)
    if mine.files + 1 > settings.UPLOAD_QUOTA_USER_FILES:
        raise UploadQuotaExceededError(
            f"You have reached the limit of {settings.UPLOAD_QUOTA_USER_FILES} stored receipts. "
            "Delete receipts you no longer need, then try again."
        )
    if mine.bytes + incoming_bytes > settings.UPLOAD_QUOTA_USER_BYTES:
        raise UploadQuotaExceededError(
            f"You have reached your receipt storage limit of "
            f"{_mb(settings.UPLOAD_QUOTA_USER_BYTES)}. Delete receipts you no longer need, "
            "then try again."
        )
    org = receipt_usage(db, organization_id=user.organization_id)
    if (
        org.files + 1 > settings.UPLOAD_QUOTA_ORG_FILES
        or org.bytes + incoming_bytes > settings.UPLOAD_QUOTA_ORG_BYTES
    ):
        raise UploadQuotaExceededError(
            "Your organisation has reached its receipt storage limit. "
            "Ask an administrator to remove old receipts."
        )
