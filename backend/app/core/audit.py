"""The audit interface — the SINGLE entry point that writes ``audit_log``.

Dev Rule 8: every state change is recorded through this one function; nothing
else inserts into ``audit_log``. The table is the immutable compliance record
(NFR-R4), so this path is **append-only** — it inserts and never updates or
deletes.

Transaction semantics: ``record_audit`` writes through the caller's session
(the P2-T1 sync session pattern) and does NOT commit. The audit row therefore
participates in the SAME transaction as the business mutation it records, so the
two commit atomically — there is no partial state where the change landed but
its audit entry did not (Dev Rule 9: fail safe, never silent). ``flush`` is
called so DB constraints surface immediately and server-side defaults (``id``,
``created_at``) are populated on the returned object.

Logging note: only non-sensitive identifiers (action, entity_type, entity_id)
are logged — never ``before_state`` / ``after_state`` payloads, which may carry
PII (TECHNICAL_DESIGN.md — no sensitive data in logs).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from database.models import AuditLog

logger = logging.getLogger(__name__)


def record_audit(
    session: Session,
    *,
    organization_id: uuid.UUID,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None = None,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one row to ``audit_log`` within the caller's transaction.

    Args:
        session: The active SQLAlchemy session for the surrounding unit of work.
        organization_id: Tenant the event belongs to (NOT NULL).
        action: Dotted event name, e.g. ``"trip.created"``.
        entity_type: Affected entity type, e.g. ``"trip"``.
        entity_id: Affected entity's UUID.
        actor_id: User who performed the action; ``None`` for system-initiated
            events (e.g. an automated approval).
        before_state: Entity snapshot before the change (``None`` on create).
        after_state: Entity snapshot after the change (``None`` on delete).

    Returns:
        The persisted (flushed, not committed) ``AuditLog`` row, with ``id`` and
        ``created_at`` populated. The caller owns the commit.
    """
    entry = AuditLog(
        organization_id=organization_id,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=before_state,
        after_state=after_state,
    )
    session.add(entry)
    session.flush()  # surface DB errors now; populate id/created_at; caller commits

    logger.info(
        "audit %s %s/%s actor=%s",
        action,
        entity_type,
        entity_id,
        actor_id if actor_id is not None else "system",
    )
    return entry
