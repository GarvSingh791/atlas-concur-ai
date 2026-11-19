"""Unit tests for the generic pagination / filter / sort helpers.

Hermetic — no DB, no routes. Exercises the library helpers directly:
clamping bounds, the page-envelope math, and the deny-by-default filter/sort
parsers (which must reject unknown fields rather than pass them through).
"""

from __future__ import annotations

import pytest

from backend.app.core.config import get_settings
from backend.app.core.errors import BadRequestError
from backend.app.core.pagination import (
    Page,
    PaginationParams,
    clamp_pagination,
    parse_filters,
    parse_sort,
)

_SETTINGS = get_settings()


# ---- Clamping ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("page_in", "expected_page"),
    [(1, 1), (5, 5), (0, 1), (-10, 1)],
)
def test_clamp_page_floored_at_one(page_in: int, expected_page: int) -> None:
    page, _ = clamp_pagination(page_in, 10)
    assert page == expected_page


def test_clamp_size_defaults_when_missing() -> None:
    _, size = clamp_pagination(1, None)
    assert size == _SETTINGS.DEFAULT_PAGE_SIZE


@pytest.mark.parametrize(
    ("size_in", "expected_size"),
    [(0, 1), (-3, 1), (10, 10)],
)
def test_clamp_size_lower_bound(size_in: int, expected_size: int) -> None:
    _, size = clamp_pagination(1, size_in)
    assert size == expected_size


def test_clamp_size_upper_bound() -> None:
    _, size = clamp_pagination(1, 10_000)
    assert size == _SETTINGS.MAX_PAGE_SIZE


def test_pagination_params_offset_and_limit() -> None:
    params = PaginationParams.create(page=3, size=25)
    assert params.page == 3
    assert params.size == 25
    assert params.limit == 25
    assert params.offset == (3 - 1) * 25


def test_pagination_params_clamps_on_create() -> None:
    params = PaginationParams.create(page=-1, size=99_999)
    assert params.page == 1
    assert params.size == _SETTINGS.MAX_PAGE_SIZE


# ---- Page envelope ----------------------------------------------------------


def test_page_create_computes_pages() -> None:
    page: Page[int] = Page.create(items=[1, 2, 3], total=45, page=2, size=20)
    assert page.items == [1, 2, 3]
    assert page.total == 45
    assert page.page == 2
    assert page.size == 20
    assert page.pages == 3  # ceil(45 / 20)


def test_page_create_zero_total() -> None:
    page: Page[int] = Page.create(items=[], total=0, page=1, size=20)
    assert page.pages == 0
    assert page.items == []


# ---- Sort parsing -----------------------------------------------------------


def test_parse_sort_empty_returns_empty() -> None:
    assert parse_sort(None, {"created_at"}) == []
    assert parse_sort("", {"created_at"}) == []


def test_parse_sort_colon_form() -> None:
    assert parse_sort("created_at:desc", {"created_at"}) == [("created_at", "desc")]


def test_parse_sort_dash_form_is_desc() -> None:
    assert parse_sort("-created_at", {"created_at"}) == [("created_at", "desc")]


def test_parse_sort_bare_field_is_asc() -> None:
    assert parse_sort("created_at", {"created_at"}) == [("created_at", "asc")]


def test_parse_sort_multiple_terms() -> None:
    result = parse_sort("status:asc,-created_at", {"status", "created_at"})
    assert result == [("status", "asc"), ("created_at", "desc")]


def test_parse_sort_rejects_unknown_field() -> None:
    with pytest.raises(BadRequestError):
        parse_sort("ssn:asc", {"created_at"})


def test_parse_sort_rejects_bad_direction() -> None:
    with pytest.raises(BadRequestError):
        parse_sort("created_at:sideways", {"created_at"})


# ---- Filter parsing ---------------------------------------------------------


def test_parse_filters_keeps_allowed_keys() -> None:
    assert parse_filters({"status": "open"}, {"status", "owner"}) == {"status": "open"}


def test_parse_filters_rejects_unknown_key() -> None:
    with pytest.raises(BadRequestError):
        parse_filters({"status": "open", "evil": "1"}, {"status"})
