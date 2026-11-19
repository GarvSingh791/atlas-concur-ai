"""Phase 11 — the app refuses to start in production with unsafe secrets.

``APP_ENV=production`` with any of these is a startup ERROR (Settings raises, so
``get_settings`` exits 1 with a message that names the variable, never its value):

* a JWT_SECRET shorter than 32 characters, or one that looks like a placeholder
  (``change-me…``, ``your-…``, the CI stub …), or one with almost no variety;
* SEED_DEFAULT_PASSWORD equal to the published demo password;
* APP_DEBUG=true or a ``*`` CORS origin.

``APP_ENV=staging`` (the public demo) only WARNS for the same findings: its demo
accounts deliberately use the published demo password, and refusing would take
the demo down. Development is untouched.

Exception (phase 12 follow-ups review fix): a non-https APP_BASE_URL is an
ERROR on staging AND production, because emailed token links are built from it.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import pytest
from pydantic import ValidationError

from config.settings import Settings
from config.settings.base import PUBLISHED_SEED_PASSWORD, deployment_secret_problems

STRONG_JWT = secrets.token_hex(32)
DB = "postgresql://u:p@db.example.internal:5432/atlas"


def _settings(**overrides: object) -> Settings:
    base: dict[str, Any] = {
        "JWT_SECRET": STRONG_JWT,
        "DATABASE_URL": DB,
        "APP_ENV": "production",
        "APP_DEBUG": False,
        "CORS_ALLOWED_ORIGINS": "https://atlas.example.com",
        # Phase 12 follow-ups: emailed links need an https frontend origin.
        "APP_BASE_URL": "https://atlas.example.com",
        "SEED_DEFAULT_PASSWORD": secrets.token_urlsafe(24),
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def test_production_with_strong_values_starts() -> None:
    s = _settings()
    assert s.APP_ENV == "production"
    assert deployment_secret_problems(s) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("JWT_SECRET", "short-secret"),
        ("JWT_SECRET", "change-me-use-a-64-char-random-hex-string"),
        ("JWT_SECRET", "ci-test-secret-not-a-real-key-do-not-use"),
        ("JWT_SECRET", "your-jwt-secret-goes-here-0123456789abcdef"),
        ("JWT_SECRET", "a" * 64),
        ("SEED_DEFAULT_PASSWORD", PUBLISHED_SEED_PASSWORD),
        ("APP_DEBUG", True),
        ("CORS_ALLOWED_ORIGINS", "*"),
        ("CORS_ALLOWED_ORIGINS", "https://atlas.example.com, *"),
    ],
)
def test_production_refuses_unsafe_value(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(**{field: value})
    message = str(exc.value)
    assert field in message
    # The message names the variable, never the value.
    if isinstance(value, str) and len(value) > 3:
        assert value not in message


def test_the_seed_password_default_is_the_published_one() -> None:
    """The constant the guard compares against is the real code default."""
    assert Settings.model_fields["SEED_DEFAULT_PASSWORD"].default == PUBLISHED_SEED_PASSWORD


def test_staging_only_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        s = _settings(
            APP_ENV="staging",
            JWT_SECRET="short-secret",
            SEED_DEFAULT_PASSWORD=PUBLISHED_SEED_PASSWORD,
        )
    assert s.APP_ENV == "staging"
    assert "JWT_SECRET" in caplog.text and "SEED_DEFAULT_PASSWORD" in caplog.text
    assert "short-secret" not in caplog.text
    assert PUBLISHED_SEED_PASSWORD not in caplog.text


def test_development_is_untouched(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="config.settings"):
        s = _settings(APP_ENV="development", JWT_SECRET="dev", APP_DEBUG=True)
    assert s.APP_ENV == "development"
    assert caplog.text == ""


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("url", ["http://atlas.example.com", "http://localhost:3000"])
def test_emailed_link_origin_must_be_https_on_staging_and_production(env: str, url: str) -> None:
    """Phase 12 follow-ups review fix: reset / invite / verify links are built
    from APP_BASE_URL, so a non-https value is a startup ERROR on staging too
    (not one of staging's warn-only findings)."""
    with pytest.raises(ValidationError) as exc:
        _settings(APP_ENV=env, APP_BASE_URL=url)
    assert "APP_BASE_URL" in str(exc.value)


def test_staging_https_base_url_with_other_findings_still_only_warns() -> None:
    s = _settings(APP_ENV="staging", SEED_DEFAULT_PASSWORD=PUBLISHED_SEED_PASSWORD)
    assert s.APP_BASE_URL == "https://atlas.example.com"


def test_development_accepts_an_http_base_url() -> None:
    s = _settings(APP_ENV="development", APP_BASE_URL="http://localhost:3000")
    assert s.APP_BASE_URL == "http://localhost:3000"
