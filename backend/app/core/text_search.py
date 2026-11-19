"""Safe substring matching for user-typed search terms (product gaps 3 / 7).

User input is never interpolated into SQL: the term is bound as a parameter and
its LIKE metacharacters (``%``, ``_`` and the escape character ``\\``) are
escaped first, so ``50%`` matches the literal text "50%" instead of acting as a
wildcard.  Matching is case-insensitive (ILIKE) and needs no extension (no
pg_trgm): callers bound the scanned rows with tenant/role scope and a LIMIT.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.sql.elements import ColumnElement

LIKE_ESCAPE = "\\"


def escape_like(term: str) -> str:
    """Escape LIKE metacharacters so *term* matches literally."""
    return (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )


def contains_ci(column: Any, term: str) -> ColumnElement[bool]:
    """``column ILIKE '%<escaped term>%' ESCAPE '\\'`` with *term* bound as a parameter."""
    return column.ilike(f"%{escape_like(term)}%", escape=LIKE_ESCAPE)
