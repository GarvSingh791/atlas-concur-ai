"""Idempotency support for Atlas mutation endpoints.

Provides three things:
  1. idempotency_key_header — FastAPI dependency that extracts the
     Idempotency-Key header, returning None if absent or raising
     BadRequestError if present-but-empty.
  2. idempotency_lookup — query the idempotency_keys table for a prior record.
  3. idempotency_record — insert a new idempotency_keys row into the current
     transaction (caller must commit).

Usage pattern (create_report example):
    1. Call idempotency_lookup before creating the entity.
       → If a prior record exists, return the already-created entity (replay).
    2. Create the entity and record_audit as normal.
    3. Call idempotency_record BEFORE session.commit() so all three rows
       (entity, audit log, idempotency key) land atomically.

Concurrent same-key races are handled by the DB unique constraint on
(organization_id, scope, idempotency_key): the second writer gets an
IntegrityError (pgcode 23505) → 409 via the global integrity_error_handler.
The client then retries and hits the lookup path, which returns the stored result.

Deferral note: receipt-upload idempotency reuses idempotency_lookup and
idempotency_record with scope="receipt.upload" in Phase 4/5 — the seam is
built here, not yet wired.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
from fastapi import Header
from sqlalchemy.orm import Session

from backend.app.core.errors import BadRequestError


def idempotency_key_header(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    """Extract the Idempotency-Key header.

    Returns None if absent (caller proceeds normally, no deduplication).
    Raises BadRequestError if the header is present but empty/whitespace.
    """
    if idempotency_key is None:
        return None
    if not idempotency_key.strip():
        raise BadRequestError("Idempotency-Key header must not be empty.")
    return idempotency_key


def idempotency_lookup(
    session: Session,
    *,
    organization_id: uuid.UUID,
    scope: str,
    key: str,
) -> uuid.UUID | None:
    """Look up a previously recorded idempotency key.

    Returns the entity_id if a matching key exists, or None if this is a
    fresh (non-duplicate) request.
    """
    from database.models.idempotency_key import IdempotencyKey

    row = session.scalars(
        sa.select(IdempotencyKey).where(
            IdempotencyKey.organization_id == organization_id,
            IdempotencyKey.scope == scope,
            IdempotencyKey.idempotency_key == key,
        )
    ).first()
    return row.entity_id if row is not None else None


def idempotency_record(
    session: Session,
    *,
    organization_id: uuid.UUID,
    scope: str,
    key: str,
    entity_id: uuid.UUID,
) -> None:
    """Record a new idempotency key -> entity mapping in the current transaction.

    Must be called BEFORE session.commit() so the key row and the entity
    land atomically.  On a concurrent same-key race the DB unique constraint
    fires an IntegrityError (pgcode 23505) -> 409 via the global handler.
    """
    from database.models.idempotency_key import IdempotencyKey

    row = IdempotencyKey(
        organization_id=organization_id,
        scope=scope,
        idempotency_key=key,
        entity_id=entity_id,
    )
    session.add(row)
