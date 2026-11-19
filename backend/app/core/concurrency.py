"""Optimistic-lock helpers for Atlas.

Provides:
  - ``if_match_version``  — FastAPI dependency that parses the If-Match header
    as an integer optimistic-lock version.
  - ``guarded_version_update`` — atomic compare-and-swap UPDATE that raises
    ConflictError when the expected version is stale.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Protocol, TypeVar

import sqlalchemy as sa
from fastapi import Header
from sqlalchemy.orm import Session

from backend.app.core.errors import BadRequestError, ConflictError


class _VersionedModel(Protocol):
    """Structural protocol for models that have id + version columns.

    Satisfied by any class mixing in UUIDPrimaryKeyMixin + VersionMixin
    (expense_reports, trips, approvals).  The attributes are SQLAlchemy
    InstrumentedAttribute instances at the class level — typed as Any here
    because the exact SA generic type is an implementation detail.
    """

    id: Any
    version: Any


_M = TypeVar("_M", bound=_VersionedModel)


def if_match_version(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int | None:
    """Parse the If-Match header as an integer optimistic-lock version.

    Returns None if the header is absent (caller uses current version).
    Raises BadRequestError on malformed value.
    """
    if if_match is None:
        return None
    stripped = if_match.strip().strip('"')  # accept bare int or quoted int
    try:
        return int(stripped)
    except ValueError:
        raise BadRequestError(
            f"If-Match header must be an integer version, got: {if_match!r}"
        ) from None


def guarded_version_update(
    session: Session,
    model: type[_M],
    *,
    entity_id: uuid.UUID,
    expected_version: int,
    values: dict[str, Any],
) -> None:
    """Atomic compare-and-swap update: only succeeds if model.version == expected_version.

    On success, increments version by 1 atomically via a single UPDATE … RETURNING.
    On failure (version mismatch or concurrent update), raises ConflictError — the
    session remains open for the get_db() rollback.

    Designed for any model with UUIDPrimaryKeyMixin + VersionMixin (trips,
    expense_reports, approvals).

    ``values`` must NOT include ``"version"`` — that key is managed internally.
    """
    if "version" in values:
        raise ValueError("guarded_version_update: 'version' must not appear in the values dict")
    result = session.execute(
        sa.update(model)
        .where(model.id == entity_id, model.version == expected_version)
        .values(**values, version=model.version + 1)
        .returning(model.version)
    )
    # rowcount is -1 with RETURNING (DBAPI undefined); fetchone() is the reliable check.
    if result.fetchone() is None:
        raise ConflictError("Record was modified concurrently; reload and retry.")
