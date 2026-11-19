"""GET /public/config exposes the demo organisation's login code in demo mode only.

The sign-in page sends it so a demo sign-in costs one bcrypt verify instead of the
five a code-less sign-in runs (timing parity); outside demo mode it is absent so
no other deployment advertises an organisation.  Hermetic: no database.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.app.api.public_config import DEMO_ORG_LOGIN_CODE
from backend.app.main_entry.app import create_app
from config.settings import get_settings

ROOT = Path(__file__).resolve().parents[2]


def test_code_matches_the_seeded_demo_organisation() -> None:
    seed = (ROOT / "database" / "seeds" / "identity_seed.py").read_text()
    assert f'name="Atlas Demo Corp", base_currency="USD", slug="{DEMO_ORG_LOGIN_CODE}"' in seed


@pytest.mark.parametrize(("demo", "expected"), [(True, DEMO_ORG_LOGIN_CODE), (False, None)])
def test_code_is_published_only_in_demo_mode(
    monkeypatch: pytest.MonkeyPatch, demo: bool, expected: str | None
) -> None:
    monkeypatch.setattr(get_settings(), "DEMO_MODE", demo)
    client = TestClient(create_app(), raise_server_exceptions=False)
    body = client.get("/api/v1/public/config").json()
    assert body["demo_mode"] is demo
    assert body["demo_org_code"] == expected
