"""UAT2 Issue 7 (hermetic) — personal lists narrow to the caller's own items.

A manager's own "My expenses" / "My Trips" pages listed their direct reports'
items because both lists return the role's full READ scope (manager = self +
direct reports, finance = org-wide).  The fix is an opt-in ``?mine=true`` that
the personal pages always send; the default scope is unchanged for every other
consumer.  These tests pin the pure narrowing helper and the router wiring; the
behavioural proof against Postgres lives in
backend/tests/api/test_my_items_scope.py.
"""

from __future__ import annotations

import inspect
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.core.rbac import narrow_to_caller
from backend.app.modules.expense import router as expense_router
from backend.app.modules.expense import schemas as expense_schemas
from backend.app.modules.travel import router as travel_router
from backend.app.modules.travel import schemas as travel_schemas
from database.models.expense_report import ExpenseReport
from database.models.identity import RoleKey, User
from database.models.trip import Trip


def _user(role: RoleKey) -> User:
    return User(id=uuid.uuid4(), organization_id=uuid.uuid4(), role=role, name="U", email="u@x")


def _sql(stmt: sa.Select) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_mine_adds_an_owner_predicate_for_reports() -> None:
    user = _user(RoleKey.manager)
    base = sa.select(ExpenseReport)
    sql = _sql(
        narrow_to_caller(base, user=user, ownership_column=ExpenseReport.owner_id, mine=True)
    )
    assert f"expense_reports.owner_id = '{user.id}'" in sql


def test_mine_adds_a_traveler_predicate_for_trips() -> None:
    user = _user(RoleKey.finance_admin)
    sql = _sql(
        narrow_to_caller(sa.select(Trip), user=user, ownership_column=Trip.traveler_id, mine=True)
    )
    assert f"trips.traveler_id = '{user.id}'" in sql


def test_mine_false_leaves_the_statement_untouched() -> None:
    user = _user(RoleKey.manager)
    base = sa.select(ExpenseReport).where(ExpenseReport.organization_id == user.organization_id)
    narrowed = narrow_to_caller(
        base, user=user, ownership_column=ExpenseReport.owner_id, mine=False
    )
    assert narrowed is base


def test_mine_only_ands_onto_an_existing_scope() -> None:
    """Narrowing is applied after scope_query — it can only add a predicate."""
    user = _user(RoleKey.manager)
    report_id = uuid.uuid4()
    scoped = sa.select(ExpenseReport).where(
        ExpenseReport.organization_id == user.organization_id,
        ExpenseReport.owner_id.in_([user.id, report_id]),
    )
    sql = _sql(
        narrow_to_caller(scoped, user=user, ownership_column=ExpenseReport.owner_id, mine=True)
    )
    assert f"expense_reports.organization_id = '{user.organization_id}'" in sql
    assert "expense_reports.owner_id IN" in sql
    assert f"expense_reports.owner_id = '{user.id}'" in sql
    assert " OR " not in sql


def test_routers_reserve_mine_so_it_never_reaches_filter_parsing() -> None:
    # parse_filters is deny-by-default: an unreserved ``mine`` would be a 400.
    assert "mine" in expense_router._RESERVED_KEYS
    assert "mine" in travel_router._RESERVED_KEYS


def test_list_endpoints_take_mine_defaulting_to_false() -> None:
    for fn in (expense_router.list_reports, travel_router.list_trips):
        param = inspect.signature(fn).parameters["mine"]
        assert param.default.default is False  # FastAPI Query(False)


def test_detail_responses_carry_the_owner_display_name() -> None:
    assert expense_schemas.ExpenseReportResponse.model_fields["owner_name"].default is None
    assert travel_schemas.TripResponse.model_fields["traveler_name"].default is None
