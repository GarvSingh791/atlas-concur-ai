"""C7-01 / C7-02 hermetic unit tests — write-scope helpers (no DB).

require_owner must refuse every non-owner regardless of role (finance_admin
included), and owner_scope_query must narrow by ownership for EVERY role (unlike
scope_query, which widens for manager / finance_admin).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.core.errors import ForbiddenError
from backend.app.core.rbac import owner_scope_query, require_owner
from backend.app.modules.travel.service import APPROVER_ONLY_TARGETS, LEGAL_TRANSITIONS
from database.models.identity import RoleKey
from database.models.trip import Trip, TripStatus


def _user(role: RoleKey) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), role=role)


@pytest.mark.parametrize("role", list(RoleKey))
def test_require_owner_allows_owner(role: RoleKey) -> None:
    user = _user(role)
    require_owner(user.id, user=user)  # no raise


@pytest.mark.parametrize("role", list(RoleKey))
def test_require_owner_refuses_non_owner_for_every_role(role: RoleKey) -> None:
    user = _user(role)
    with pytest.raises(ForbiddenError):
        require_owner(uuid.uuid4(), user=user)


def test_require_owner_refuses_none() -> None:
    user = _user(RoleKey.finance_admin)
    with pytest.raises(ForbiddenError):
        require_owner(None, user=user)


@pytest.mark.parametrize("role", list(RoleKey))
def test_owner_scope_query_filters_by_owner_and_org_for_every_role(role: RoleKey) -> None:
    user = _user(role)
    stmt = owner_scope_query(
        sa.select(Trip),
        user=user,
        ownership_column=Trip.traveler_id,
        org_column=Trip.organization_id,
    )
    compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "trips.traveler_id = " in sql and str(user.id) in sql
    assert "trips.organization_id = " in sql and str(user.organization_id) in sql
    assert " IN " not in sql  # never widened to direct reports


def test_trip_decision_targets_are_approver_only() -> None:
    assert APPROVER_ONLY_TARGETS == frozenset({TripStatus.approved, TripStatus.rejected})
    # They remain legal state-machine edges (the approval workflow owns them);
    # the owner PATCH path refuses them.
    assert APPROVER_ONLY_TARGETS <= LEGAL_TRANSITIONS[TripStatus.pending_approval]
