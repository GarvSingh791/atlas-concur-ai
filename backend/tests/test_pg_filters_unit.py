"""Product gap 6 (hermetic) — exception-type catalog, filter value parsing, wiring.

The behavioural proofs against Postgres live in backend/tests/api/test_pg_finance_filters.py.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.core.errors import BadRequestError
from backend.app.modules.approval import filters
from backend.app.modules.approval import router as approval_router
from database.models.approval import Approval

REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Exception-type catalog
# ---------------------------------------------------------------------------


def test_catalog_covers_every_flag_type_the_producers_emit() -> None:
    """Every ``flag_type="..."`` literal / ``*_FLAG = "..."`` constant in the agent
    layer is a filterable exception type — a new producer flag cannot silently
    become unfilterable."""
    emitted: set[str] = set()
    for path in (REPO / "ai_agents").rglob("*.py"):
        if "tests" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        emitted |= set(re.findall(r'flag_type="([a-z_]+)"', src))
        emitted |= set(re.findall(r'^[A-Z_]*_FLAG = "([a-z_]+)"', src, flags=re.M))
    emitted.discard("")
    assert emitted, "scan found nothing — the pattern is stale"
    assert emitted <= set(filters.EXCEPTION_TYPES), emitted - set(filters.EXCEPTION_TYPES)


def test_catalog_covers_every_policy_rule_type() -> None:
    from database.models.policy_rule import PolicyRuleType

    for rt in PolicyRuleType:
        assert filters.EXCEPTION_TYPES[rt.value].kind == filters.KIND_RULE


def test_exception_types_of_matches_flags_rules_and_segments() -> None:
    risk = {
        "flags": [
            {"flag_type": "policy_violation", "rule_type": "per_diem"},
            {"segment_id": "s", "in_policy": False},
            {"segment_id": "t", "in_policy": True},
            {"flag_type": "brand_new_unknown"},
            "garbage",
        ]
    }
    report = {"flags": [{"flag_type": "missing_receipt"}]}
    assert filters.exception_types_of(risk, report, None, {"flags": "x"}) == [
        "policy_violation",
        "missing_receipt",
        "per_diem",
        "out_of_policy_segment",
    ]
    assert filters.exception_labels(["missing_receipt", "nope"]) == ["Missing receipt"]


def test_exception_filter_is_a_bound_jsonb_containment() -> None:
    etype = filters.parse_exception_type("amount_cap")
    stmt = sa.select(Approval.id).where(filters.approval_exception_predicate(etype))
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "@>" in sql
    assert "amount_cap" not in sql  # the value is a bound parameter, never inlined


@pytest.mark.parametrize("value", ["", "AMOUNT_CAP", "amount_cap;drop", "flags"])
def test_unknown_exception_type_is_400(value: str) -> None:
    with pytest.raises(BadRequestError):
        filters.parse_exception_type(value)


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------


def test_date_bounds_make_the_to_date_inclusive() -> None:
    d = filters.parse_iso_date("submitted_to", "2026-02-28")
    assert filters.day_start(d) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert filters.day_end_exclusive(d) == datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", ["2026-02-30", "2026-1-01", "20260101", "2026-01-01T00:00"])
def test_bad_dates_are_400(value: str) -> None:
    with pytest.raises(BadRequestError):
        filters.parse_iso_date("submitted_from", value)


def test_money_is_decimal_and_validated() -> None:
    assert filters.parse_money("amount_min", " 10.5 ") == Decimal("10.5")
    for bad in ("-1", "1e3", "NaN", "10.123", "", "1,000"):
        with pytest.raises(BadRequestError):
            filters.parse_money("amount_min", bad)


def test_ranges_are_checked() -> None:
    stmt = sa.select(Approval)
    with pytest.raises(BadRequestError):
        filters.apply_approval_extended_filters(
            stmt, {"amount_min": "5", "amount_max": "4"}, organization_id=uuid.uuid4()
        )


def test_split_filters() -> None:
    core, ext = filters.split_filters(
        {"current_status": "pending", "department": "Ops"}, filters.APPROVAL_EXTENDED_FILTER_FIELDS
    )
    assert core == {"current_status": "pending"} and ext == {"department": "Ops"}


def test_filter_option_routes_are_declared_before_the_id_route() -> None:
    paths = [getattr(r, "path", "") for r in approval_router.router.routes]
    detail = paths.index("/approvals/{approval_id}")
    for literal in ("/approvals/exception-types", "/approvals/departments"):
        assert paths.index(literal) < detail, literal
