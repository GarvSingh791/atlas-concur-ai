"""Phase 12 follow-ups — account recovery, hermetic parts.

Token hashing / shape, link building, path redaction for logs, the session
fingerprint (password version) and the rate-limit route classification.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.core.errors import UnauthorizedError, loggable_path
from backend.app.core.rate_limit import classify
from backend.app.core.security import (
    PASSWORD_VERSION_CLAIM,
    create_access_token,
    decode_token,
    ensure_token_current,
    password_version,
)
from backend.app.modules.identity import accounts
from backend.app.modules.identity.password_policy import (
    has_usable_password,
    unusable_password_hash,
)
from config.settings import get_settings


def test_tokens_are_32_random_bytes_and_stored_as_sha256_hex() -> None:
    raw, digest = accounts.new_token()
    assert len(raw) == 43 and accounts._plausible(raw)
    assert digest == accounts.hash_token(raw) and len(digest) == 64
    assert raw not in digest
    assert accounts.new_token()[0] != raw


@pytest.mark.parametrize("raw", ["", "short", "A" * 200, "A" * 42 + "/", "A" * 42 + "%"])
def test_implausible_tokens_are_rejected_before_any_lookup(raw: str) -> None:
    assert not accounts._plausible(raw)


def test_links_use_app_base_url_and_encode_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "APP_BASE_URL", "https://app.example.test/atlas")
    link = accounts.build_link(accounts.RESET_PATH, "abc-_123")
    assert link == "https://app.example.test/atlas/reset-password?token=abc-_123"


def test_email_bodies_escape_user_controlled_names(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(name='<script>alert("x")</script>', email="a@example.test")
    message = accounts.reset_message(user, "https://app.example.test/reset-password?token=t")  # type: ignore[arg-type]
    assert "<script>" not in message.html_body and "&lt;script&gt;" in message.html_body
    assert message.purpose == "reset" and message.to == "a@example.test"


@pytest.mark.parametrize(
    ("path", "logged"),
    [
        ("/api/v1/auth/invite/SeCrEt-token_123", "/api/v1/auth/invite/[redacted]"),
        ("/api/v1/auth/invite/abc/extra", "/api/v1/auth/invite/[redacted]/extra"),
        ("/api/v1/users/123", "/api/v1/users/123"),
        ("/api/v1/auth/accept-invite", "/api/v1/auth/accept-invite"),
    ],
)
def test_logged_paths_redact_invite_tokens(path: str, logged: str) -> None:
    assert loggable_path(path) == logged


def test_unusable_password_marks_an_invited_account() -> None:
    marker = unusable_password_hash()
    assert not has_usable_password(marker)
    assert marker != unusable_password_hash()
    assert has_usable_password("$2b$12$abcdefghijklmnopqrstuv")
    assert not has_usable_password("")


# ---------------------------------------------------------------------------
# Session fingerprint
# ---------------------------------------------------------------------------


def _user(changed_at: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), organization_id=uuid.uuid4(), password_changed_at=changed_at
    )


def test_password_version_is_timezone_independent_and_keyed() -> None:
    uid = uuid.uuid4()
    utc = datetime(2026, 11, 15, 10, 0, 0, 123456, tzinfo=timezone.utc)
    other = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert password_version(uid, utc) == password_version(uid, other)
    assert password_version(uid, None) is None
    assert password_version(uid, utc) != password_version(uid, utc + timedelta(microseconds=1))
    assert password_version(uuid.uuid4(), utc) != password_version(uid, utc)
    assert str(int(utc.timestamp())) not in str(password_version(uid, utc))


def test_a_token_minted_before_a_password_change_is_refused() -> None:
    user = _user(None)
    legacy = decode_token(
        create_access_token(user.id, user.organization_id), expected_type="access"
    )
    assert PASSWORD_VERSION_CLAIM not in legacy
    ensure_token_current(legacy, user)  # never changed: legacy tokens still work

    user.password_changed_at = datetime.now(timezone.utc)
    with pytest.raises(UnauthorizedError):
        ensure_token_current(legacy, user)  # fail closed: no claim, but a change

    current = decode_token(
        create_access_token(
            user.id, user.organization_id, password_changed_at=user.password_changed_at
        ),
        expected_type="access",
    )
    ensure_token_current(current, user)
    user.password_changed_at += timedelta(seconds=1)
    with pytest.raises(UnauthorizedError):
        ensure_token_current(current, user)  # changed again
    with pytest.raises(UnauthorizedError):
        ensure_token_current({**current, PASSWORD_VERSION_CLAIM: 12345}, user)


# ---------------------------------------------------------------------------
# Rate-limit classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "policy"),
    [
        ("POST", "/auth/forgot-password", "auth_forgot_password"),
        ("POST", "/auth/reset-password", "auth_token"),
        ("POST", "/auth/accept-invite", "auth_token"),
        ("POST", "/auth/verify-email", "auth_token"),
        ("POST", "/auth/change-password", "auth_token"),
        ("GET", "/auth/invite/sometoken", "auth_invite_peek"),
        ("POST", "/auth/send-verification", "auth_send_verification"),
        ("POST", "/users/invite", "user_invite"),
        ("POST", f"/users/{uuid.uuid4()}/invite/resend", "user_invite"),
        ("POST", f"/users/{uuid.uuid4()}/invite/revoke", "user_invite"),
        ("POST", "/users", "user_admin"),
        ("POST", "/auth/login", "auth_login"),
    ],
)
def test_recovery_routes_have_their_own_policies(method: str, path: str, policy: str) -> None:
    assert classify(method, path) == policy


def test_verify_password_does_real_bcrypt_work_for_a_non_bcrypt_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review fix: a malformed / unusable stored hash must not short-circuit
    (bcrypt raises on it in microseconds -> timing oracle).  verify_password
    falls back to a full check against DUMMY_HASH and still returns False."""
    from backend.app.core import security

    calls: list[bytes] = []
    real = security._bcrypt.checkpw

    def recording(pw: bytes, hashed: bytes) -> bool:
        calls.append(hashed)
        return real(pw, hashed)

    monkeypatch.setattr(security._bcrypt, "checkpw", recording)
    assert security.verify_password("__atlas_dummy__", unusable_password_hash()) is False
    assert security.DUMMY_HASH.encode() in calls
    calls.clear()
    assert security.verify_password("anything", "not-a-hash") is False
    assert calls[-1] == security.DUMMY_HASH.encode()
