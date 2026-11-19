"""C3 — build_agent_context correlation_id resolution (hermetic, no DB).

build_agent_context is the single backend→ai_agents bridge that builds the
AgentContext every AI call site uses; its correlation_id flows verbatim into
ai_decision_logs.correlation_id via write_decision_log. These tests pin the
resolution rules:

  1. Inside a request (ContextVar set) the ContextVar value is used.
  2. An explicit correlation_id argument wins over the ContextVar.
  3. Outside a request (ContextVar default "") the result is None, never "".
  4. An explicit "" with no ContextVar value normalizes to None.
  5. An explicit "" inside a request falls back to the ContextVar.
  6. An oversized (untrusted header) value is truncated to 128 chars.

get_user_permissions is monkeypatched so no Session/DB is touched.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

import backend.app.modules.ai.runtime as runtime
from backend.app.core.correlation import correlation_id_var
from backend.app.modules.ai.runtime import MAX_CORRELATION_ID_LENGTH, build_agent_context
from database.models.identity import User


@pytest.fixture(autouse=True)
def _no_rbac_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the RBAC DB query with a fixed permission list."""
    monkeypatch.setattr(runtime, "get_user_permissions", lambda user, session: ["policy.read"])


@contextmanager
def _request_cid(value: str) -> Iterator[None]:
    """Simulate CorrelationIDMiddleware: set the ContextVar, reset on exit."""
    token = correlation_id_var.set(value)
    try:
        yield
    finally:
        correlation_id_var.reset(token)


def _user() -> User:
    return cast(
        User,
        SimpleNamespace(organization_id=uuid.uuid4(), id=uuid.uuid4(), role="employee"),
    )


_SESSION: Any = object()


class TestBuildAgentContextCorrelation:
    def test_contextvar_used_when_no_explicit_arg(self) -> None:
        with _request_cid("cv-1"):
            ctx = build_agent_context(_user(), _SESSION)
        assert ctx.correlation_id == "cv-1"

    def test_explicit_arg_wins_over_contextvar(self) -> None:
        with _request_cid("cv-1"):
            ctx = build_agent_context(_user(), _SESSION, correlation_id="explicit-1")
        assert ctx.correlation_id == "explicit-1"

    def test_outside_request_is_none(self) -> None:
        assert correlation_id_var.get() == ""
        ctx = build_agent_context(_user(), _SESSION)
        assert ctx.correlation_id is None

    def test_empty_explicit_outside_request_is_none(self) -> None:
        ctx = build_agent_context(_user(), _SESSION, correlation_id="")
        assert ctx.correlation_id is None

    def test_empty_explicit_inside_request_falls_back_to_contextvar(self) -> None:
        with _request_cid("cv-2"):
            ctx = build_agent_context(_user(), _SESSION, correlation_id="")
        assert ctx.correlation_id == "cv-2"

    def test_oversized_contextvar_value_truncated(self) -> None:
        long_cid = "x" * (MAX_CORRELATION_ID_LENGTH + 500)
        with _request_cid(long_cid):
            ctx = build_agent_context(_user(), _SESSION)
        assert ctx.correlation_id == "x" * MAX_CORRELATION_ID_LENGTH
        assert MAX_CORRELATION_ID_LENGTH == 128

    def test_oversized_explicit_value_truncated(self) -> None:
        ctx = build_agent_context(_user(), _SESSION, correlation_id="y" * 129)
        assert ctx.correlation_id == "y" * 128

    def test_exact_limit_value_kept_intact(self) -> None:
        cid = "z" * MAX_CORRELATION_ID_LENGTH
        ctx = build_agent_context(_user(), _SESSION, correlation_id=cid)
        assert ctx.correlation_id == cid

    def test_other_fields_unchanged(self) -> None:
        user = _user()
        with _request_cid("cv-3"):
            ctx = build_agent_context(user, _SESSION)
        assert ctx.organization_id == user.organization_id
        assert ctx.user_id == user.id
        assert ctx.role == "employee"
        assert ctx.granted_permission_keys == frozenset({"policy.read"})
