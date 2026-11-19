"""Product gap 8 (hermetic) — CSV helpers: RFC-4180, BOM, injection guard, cap marker.

The behavioural proofs against Postgres live in backend/tests/api/test_pg_csv_export.py.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

import pytest

from backend.app.core import csv_export
from backend.app.modules.approval import router as approval_router


@pytest.mark.parametrize("payload", ["=1+1", "+cmd", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_sanitize_prefixes_formula_triggers(payload: str) -> None:
    assert csv_export.sanitize_cell(payload) == "'" + payload


@pytest.mark.parametrize("payload", ["Hello", "a=b", " =x", "", "Ünïcødé"])
def test_sanitize_leaves_plain_text(payload: str) -> None:
    assert csv_export.sanitize_cell(payload) == payload


def test_sanitize_none_and_server_formatted_cells() -> None:
    assert csv_export.sanitize_cell(None) == ""
    assert csv_export.sanitize_cell(csv_export.Raw("-5.00")) == "-5.00"
    assert csv_export.money_cell(Decimal("12.5")) == "12.50"
    assert csv_export.money_cell(None) == ""
    assert csv_export.bool_cell(True) == "yes"
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert csv_export.timestamp_cell(naive) == "2026-01-02T03:04:05Z"
    assert csv_export.timestamp_cell(date(2026, 1, 2)) == "2026-01-02"


def test_iter_csv_is_rfc4180_with_bom_and_marker() -> None:
    chunks = list(
        csv_export.iter_csv(
            ["A", "B"], [['x, "y"', "=evil"], ["line\nbreak", None]], truncated=True, cap=2
        )
    )
    text = "".join(chunks)
    assert text.startswith("﻿A,B\r\n")
    assert '"x, ""y""",\'=evil\r\n' in text
    rows = list(csv.reader(io.StringIO(text[1:])))
    assert rows == [
        ["A", "B"],
        ['x, "y"', "'=evil"],
        ["line\nbreak", ""],
        [
            csv_export.TRUNCATION_MARKER,
            "Export limited to 2 rows; narrow the filters to export the rest.",
        ],
    ]


def test_iter_csv_without_truncation_has_no_marker() -> None:
    text = "".join(csv_export.iter_csv(["A"], [["1"]], truncated=False, cap=5))
    assert csv_export.TRUNCATION_MARKER not in text


def test_export_filename_is_dated() -> None:
    assert csv_export.export_filename("approvals", date(2026, 11, 14)) == (
        "atlas-approvals-2026-11-14.csv"
    )


def test_export_route_is_declared_before_the_id_route() -> None:
    paths = [getattr(r, "path", "") for r in approval_router.router.routes]
    assert paths.index("/approvals/export.csv") < paths.index("/approvals/{approval_id}")
