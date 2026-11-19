"""P8-T4 — the commit reported by GET /health is read through config/settings.

Render injects ``RENDER_GIT_COMMIT`` at runtime on Git-built services; the
``APP_COMMIT_SHA`` setting maps it (and lets an explicit APP_COMMIT_SHA win).
These tests build ``Settings`` directly, so the cached ``get_settings()`` used
by the app is never polluted.
"""

from __future__ import annotations

import pytest

from config.settings.base import Settings

_REQUIRED = {
    "JWT_SECRET": "unit-test-secret-not-a-real-key-0123456789",
    "DATABASE_URL": "postgresql+psycopg2://atlas:atlas@localhost:5432/atlas_test",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key in ("APP_COMMIT_SHA", "RENDER_GIT_COMMIT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**_REQUIRED, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_commit_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _settings(monkeypatch).APP_COMMIT_SHA == "local"


def test_commit_read_from_render_git_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert _settings(monkeypatch, RENDER_GIT_COMMIT=sha).APP_COMMIT_SHA == sha


def test_explicit_app_commit_sha_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, APP_COMMIT_SHA="override", RENDER_GIT_COMMIT="render-sha")
    assert s.APP_COMMIT_SHA == "override"
