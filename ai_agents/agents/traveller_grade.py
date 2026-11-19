"""The traveller's grade of record (AIR-04) — read from the database, never from a request.

One lookup every AIR-04 consumer shares: the travel planner (the requesting traveller,
``context.user_id``), trip approval (the trip's ``traveler_id``) and expense-line
evaluation (the report OWNER — not whoever asks for the evaluation, e.g. an approver
on POST /policy/evaluate).  Org-scoped; a user of another organization, a missing
user or a value that is not on the ladder yields None (unknown -> AIR-04 is never
granted automatically).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.constants import grades as grade_ladder
from database.models.identity import User

logger = logging.getLogger(__name__)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def owner_grade(session: Session, *, organization_id: Any, user_id: Any) -> str | None:
    """``users.grade`` of *user_id* in *organization_id*, or None (unknown).

    Fails closed: any lookup error is logged (ids only) and treated as unknown.
    """
    org, uid = _as_uuid(organization_id), _as_uuid(user_id)
    if org is None or uid is None:
        return None
    try:
        raw = session.scalar(
            sa.select(User.grade).where(User.id == uid, User.organization_id == org)
        )
    except Exception as exc:  # a failed lookup must never grant an exception
        logger.warning("traveller_grade: lookup failed for user %s: %s", uid, type(exc).__name__)
        return None
    return raw if grade_ladder.grade(raw) is not None else None
