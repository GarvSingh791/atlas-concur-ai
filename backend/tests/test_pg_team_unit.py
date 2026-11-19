"""Product gap 5 (hermetic) — the team read gate + fail-closed scope.

The behavioural proofs against Postgres live in backend/tests/api/test_pg_team_view.py.
"""

from __future__ import annotations

import uuid

import pytest

from backend.app.core.errors import ForbiddenError
from backend.app.modules.team import router as team_router
from database.models.identity import RoleKey, User


def _user(role: RoleKey) -> User:
    return User(id=uuid.uuid4(), organization_id=uuid.uuid4(), role=role, name="U", email="u@x")


@pytest.mark.parametrize(
    ("perms", "allowed"),
    [
        ({"expense.read.reports"}, True),
        ({"expense.read.org"}, True),
        ({"expense.read.own", "approval.read", "users.read"}, False),
        (set(), False),
    ],
)
def test_team_gate_needs_a_team_read_permission(
    monkeypatch: pytest.MonkeyPatch, perms: set[str], allowed: bool
) -> None:
    monkeypatch.setattr(team_router, "get_user_permissions", lambda _u, _s: perms)
    user = _user(RoleKey.manager)
    if allowed:
        assert team_router.require_team_reader(user, session=None) is user  # type: ignore[arg-type]
    else:
        with pytest.raises(ForbiddenError):
            team_router.require_team_reader(user, session=None)  # type: ignore[arg-type]


def test_team_scope_fails_closed_for_other_roles() -> None:
    from backend.app.modules.team.service import team_member_ids

    with pytest.raises(ForbiddenError):
        team_member_ids(None, _user(RoleKey.employee))  # type: ignore[arg-type]
