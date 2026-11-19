"""Phase 11 — the identity seed never prints the demo password.

The seed runs in CI (E2E lane) and on Render; its stdout lands in job / deploy
logs. It used to print ``Password : <SEED_DEFAULT_PASSWORD>``.
"""

from __future__ import annotations

import contextlib
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from config.settings import get_settings
from database.seeds import identity_seed

pytestmark = pytest.mark.db


def test_identity_seed_output_does_not_contain_the_password(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Move any already-seeded demo org aside (rolled back with the test) so the
    # full org + users block — the one that printed the password — runs.
    db_session.execute(
        sa.text(
            "UPDATE organizations SET name = name || ' (moved)', slug = slug || '-moved' "
            "WHERE name = 'Atlas Demo Corp'"
        )
    )
    distinct_pw = "ps-ops-distinct-seed-pw-7f3a9c"
    patched = get_settings().model_copy(update={"SEED_DEFAULT_PASSWORD": distinct_pw})
    monkeypatch.setattr(identity_seed, "get_settings", lambda: patched)

    def factory() -> Any:
        return contextlib.nullcontext(db_session)

    identity_seed.seed(session_factory=factory)
    out = capsys.readouterr().out
    assert "Identity seed complete." in out
    assert distinct_pw not in out
