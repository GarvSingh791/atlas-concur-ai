"""Product gaps 3 / 7 — LIKE-escaping helper for user-typed search terms (hermetic)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from backend.app.core.text_search import contains_ci, escape_like


def test_escape_like_escapes_every_metacharacter() -> None:
    assert escape_like("50%") == "50\\%"
    assert escape_like("a_b") == "a\\_b"
    assert escape_like("back\\slash") == "back\\\\slash"
    # The escape character is doubled FIRST, so an escaped wildcard is not re-escaped.
    assert escape_like("\\%") == "\\\\\\%"
    assert escape_like("plain text") == "plain text"


def test_contains_ci_binds_the_term_as_a_parameter() -> None:
    col = sa.column("title", sa.Text)
    expr = contains_ci(col, "x'; DROP TABLE users; --%")
    compiled = expr.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "ILIKE" in sql.upper()
    assert "DROP TABLE" not in sql  # value never inlined into the SQL text
    assert "ESCAPE" in sql.upper()
    (value,) = compiled.params.values()
    assert value == "%x'; DROP TABLE users; --\\%%"
