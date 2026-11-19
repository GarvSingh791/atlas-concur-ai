"""Generic pagination / filtering / sorting helpers.

Library-only utilities applied uniformly across collection endpoints
(``TECHNICAL_DESIGN.md`` — API Design Principles: "consistent envelopes,
pagination, filtering, sorting applied uniformly"). There are NO domain list
endpoints yet, so these are validated by unit tests rather than a live route.

Pagination style: 1-based ``page`` + ``size``. Out-of-range requests are
**clamped** (never rejected) to ``Settings.DEFAULT_PAGE_SIZE`` /
``Settings.MAX_PAGE_SIZE`` (config-driven; Dev Rule 12). Filter/sort parsing is
**deny-by-default**: any field not in the caller-supplied allow-set is rejected
with a ``BadRequestError`` — arbitrary client input is never passed through to
the query layer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil
from typing import Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel

from backend.app.core.config import get_settings
from backend.app.core.errors import BadRequestError

T = TypeVar("T")

_VALID_DIRECTIONS = ("asc", "desc")


# ---- Pagination params ------------------------------------------------------


def clamp_pagination(page: int, size: int | None) -> tuple[int, int]:
    """Clamp a requested (page, size) into sane, config-driven bounds.

    ``page`` is floored at 1. ``size`` defaults to ``DEFAULT_PAGE_SIZE`` when not
    supplied, then is clamped into ``[1, MAX_PAGE_SIZE]``.
    """
    settings = get_settings()
    clamped_page = page if page >= 1 else 1

    effective_size = size if size is not None else settings.DEFAULT_PAGE_SIZE
    if effective_size < 1:
        effective_size = 1
    elif effective_size > settings.MAX_PAGE_SIZE:
        effective_size = settings.MAX_PAGE_SIZE

    return clamped_page, effective_size


@dataclass(frozen=True)
class PaginationParams:
    """Validated, clamped pagination inputs with SQL-friendly derivations."""

    page: int
    size: int

    @property
    def offset(self) -> int:
        """Zero-based row offset for the current page."""
        return (self.page - 1) * self.size

    @property
    def limit(self) -> int:
        """Row limit — alias of ``size`` for use in queries."""
        return self.size

    @classmethod
    def create(cls, *, page: int, size: int | None) -> PaginationParams:
        clamped_page, clamped_size = clamp_pagination(page, size)
        return cls(page=clamped_page, size=clamped_size)


def paginated(
    page: int = Query(1, description="1-based page number; clamped to >= 1."),
    size: int | None = Query(None, description="Items per page; clamped to [1, MAX_PAGE_SIZE]."),
) -> PaginationParams:
    """FastAPI dependency yielding clamped ``PaginationParams`` (for future routes)."""
    return PaginationParams.create(page=page, size=size)


# ---- Paginated response envelope --------------------------------------------


class Page(BaseModel, Generic[T]):
    """Standard paginated response envelope."""

    items: list[T]
    total: int
    page: int
    size: int
    pages: int

    @classmethod
    def create(cls, *, items: list[T], total: int, page: int, size: int) -> Page[T]:
        pages = ceil(total / size) if size > 0 else 0
        return cls(items=items, total=total, page=page, size=size, pages=pages)


# ---- Safe filter / sort parsing (deny-by-default) ---------------------------


def parse_sort(raw: str | None, allowed: set[str]) -> list[tuple[str, str]]:
    """Parse a sort spec into ``[(field, direction)]``, rejecting unknown fields.

    Accepts comma-separated terms in either ``field:asc|desc`` or ``-field``
    (descending) / ``field`` (ascending) form. Raises ``BadRequestError`` for any
    field not in ``allowed`` or any direction outside ``asc``/``desc``.
    """
    if not raw:
        return []

    parsed: list[tuple[str, str]] = []
    for term in raw.split(","):
        term = term.strip()
        if not term:
            continue

        if ":" in term:
            field, _, direction = term.partition(":")
            field = field.strip()
            direction = direction.strip().lower()
        elif term.startswith("-"):
            field = term[1:].strip()
            direction = "desc"
        else:
            field = term
            direction = "asc"

        if field not in allowed:
            raise BadRequestError(f"Cannot sort by unknown field '{field}'.")
        if direction not in _VALID_DIRECTIONS:
            raise BadRequestError(f"Invalid sort direction '{direction}' for field '{field}'.")
        parsed.append((field, direction))

    return parsed


def parse_filters(raw: Mapping[str, str], allowed: set[str]) -> dict[str, str]:
    """Return the subset of ``raw`` whose keys are in ``allowed``.

    Raises ``BadRequestError`` if any key is not in ``allowed`` — arbitrary
    filter fields are never silently dropped or passed through. Value coercion
    (types, operators) is the domain endpoint's responsibility, not this helper's.
    """
    unknown = set(raw) - allowed
    if unknown:
        raise BadRequestError(f"Unknown filter field(s): {', '.join(sorted(unknown))}.")
    return dict(raw)
