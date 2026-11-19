"""Email provider (phase 12 follow-ups: account recovery) — hermetic.

* console: one masked line per message; never the link, token, body or the
  full recipient address;
* smtp: multipart text + HTML, TLS / timeouts wired, failures swallowed and
  logged without secrets (the API must answer the same way either way);
* factory selects the configured provider;
* settings: smtp needs a host and a sender, APP_BASE_URL must be an absolute
  http(s) origin and https outside development.
"""

from __future__ import annotations

import logging
import secrets
import smtplib
from typing import Any

import pytest
from pydantic import ValidationError

from config.settings import Settings, get_settings
from config.settings.base import deployment_secret_problems
from services.email_provider import factory
from services.email_provider.console import ConsoleEmailProvider, OffEmailProvider
from services.email_provider.interface import EmailMessage, mask_address
from services.email_provider.smtp import SmtpEmailProvider

TOKEN = "tok_" + "x" * 40
LINK = f"https://app.example.test/reset-password?token={TOKEN}"
TO = "alice.liddell@wonderland.example.com"


def _message(purpose: str = "reset") -> EmailMessage:
    return EmailMessage(
        to=TO,
        subject="Reset your Atlas password",
        text_body=f"Open {LINK}",
        html_body=f'<a href="{LINK}">Reset</a>',
        purpose=purpose,
    )


# ---------------------------------------------------------------------------
# masking + console
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("alice@domain.com", "a***@d***.com"),
        ("Bob@Sub.Example.CO", "B***@S***.CO"),
        ("no-at-sign", "***"),
        ("x@localhost", "x***@***"),
        ("", "***"),
    ],
)
def test_mask_address(address: str, masked: str) -> None:
    assert mask_address(address) == masked


def test_message_repr_hides_recipient_and_bodies() -> None:
    text = repr(_message())
    assert TO not in text and TOKEN not in text and LINK not in text


def test_console_logs_one_masked_line_and_never_the_link(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert ConsoleEmailProvider().send(_message()) is True
    assert caplog.text.count("email not delivered") == 1
    assert "provider=console purpose=reset to=a***@w***.com" in caplog.text
    for secret in (TOKEN, LINK, TO, "alice.liddell", "wonderland"):
        assert secret not in caplog.text
    assert ConsoleEmailProvider().delivers is False


def test_off_provider_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert OffEmailProvider().send(_message()) is True
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# smtp
# ---------------------------------------------------------------------------


class _FakeSmtp:
    instances: list[_FakeSmtp] = []
    fail_on: str | None = None

    def __init__(self, host: str, port: int, timeout: float | None = None, **kw: Any) -> None:
        self.host, self.port, self.timeout, self.kw = host, port, timeout, kw
        self.calls: list[str] = []
        self.sent: list[Any] = []
        _FakeSmtp.instances.append(self)

    def _maybe_fail(self, step: str) -> None:
        self.calls.append(step)
        if _FakeSmtp.fail_on == step:
            raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {TO}".encode())

    def ehlo(self) -> None:
        self._maybe_fail("ehlo")

    def starttls(self, context: Any = None) -> None:
        self.kw["starttls_context"] = context
        self._maybe_fail("starttls")

    def login(self, user: str, password: str) -> None:
        self._maybe_fail("login")

    def send_message(self, mime: Any) -> None:
        self._maybe_fail("send")
        self.sent.append(mime)

    def quit(self) -> None:
        self.calls.append("quit")

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture()
def fake_smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSmtp]:
    _FakeSmtp.instances = []
    _FakeSmtp.fail_on = None
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSmtp)
    return _FakeSmtp


def _smtp(**kw: Any) -> SmtpEmailProvider:
    args: dict[str, Any] = {
        "host": "smtp.example.test",
        "port": 587,
        "sender": "Atlas <no-reply@example.test>",
        "username": "mailer",
        "password": "s3cret-smtp-password",
        "use_tls": True,
        "timeout": 7.0,
    }
    args.update(kw)
    return SmtpEmailProvider(**args)


def test_smtp_sends_text_and_html_over_starttls(fake_smtp: type[_FakeSmtp]) -> None:
    provider = _smtp()
    assert provider.delivers is True
    assert provider.send(_message()) is True
    (client,) = fake_smtp.instances
    assert (client.host, client.port, client.timeout) == ("smtp.example.test", 587, 7.0)
    assert client.calls == ["ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert client.kw["starttls_context"] is not None
    (mime,) = client.sent
    assert mime["To"] == TO and mime["From"] == "Atlas <no-reply@example.test>"
    types = [part.get_content_type() for part in mime.iter_parts()]
    assert types == ["text/plain", "text/html"]
    assert LINK in mime.get_body(("plain",)).get_content()
    assert "s3cret-smtp-password" not in repr(provider)


def test_smtp_port_465_uses_implicit_tls(fake_smtp: type[_FakeSmtp]) -> None:
    assert _smtp(port=465).send(_message()) is True
    (client,) = fake_smtp.instances
    assert "starttls" not in client.calls
    assert client.kw.get("context") is not None


def test_smtp_without_tls_never_upgrades(fake_smtp: type[_FakeSmtp]) -> None:
    assert _smtp(use_tls=False, username="").send(_message()) is True
    (client,) = fake_smtp.instances
    assert client.calls == ["send", "quit"]


@pytest.mark.parametrize("step", ["starttls", "login", "send"])
def test_smtp_failure_is_swallowed_and_logged_without_secrets(
    fake_smtp: type[_FakeSmtp], step: str, caplog: pytest.LogCaptureFixture
) -> None:
    fake_smtp.fail_on = step
    with caplog.at_level(logging.DEBUG):
        assert _smtp().send(_message()) is False
    assert "email delivery failed: provider=smtp purpose=reset" in caplog.text
    assert "SMTPAuthenticationError" in caplog.text
    for secret in (TOKEN, LINK, TO, "s3cret-smtp-password", "bad credentials"):
        assert secret not in caplog.text


def test_smtp_connection_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(smtplib, "SMTP", _refuse)
    assert _smtp().send(_message()) is False


# ---------------------------------------------------------------------------
# factory + settings
# ---------------------------------------------------------------------------


def _with(monkeypatch: pytest.MonkeyPatch, **update: Any) -> None:
    patched = get_settings().model_copy(update=update)
    monkeypatch.setattr(factory, "get_settings", lambda: patched)


def test_factory_selects_the_configured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _with(monkeypatch, EMAIL_PROVIDER="console")
    assert isinstance(factory.get_email_provider(), ConsoleEmailProvider)
    _with(monkeypatch, EMAIL_PROVIDER="off")
    assert isinstance(factory.get_email_provider(), OffEmailProvider)
    _with(monkeypatch, EMAIL_PROVIDER="smtp", SMTP_HOST="smtp.x.test", EMAIL_FROM="a@x.test")
    assert isinstance(factory.get_email_provider(), SmtpEmailProvider)


def test_code_default_is_console() -> None:
    assert Settings.model_fields["EMAIL_PROVIDER"].default == "console"
    assert Settings.model_fields["AUTH_RESET_LINK_TTL_MINUTES"].default == 30
    assert Settings.model_fields["AUTH_INVITE_LINK_TTL_HOURS"].default == 7 * 24


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"JWT_SECRET": "x" * 40, "DATABASE_URL": "postgresql://u@h/d"}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "missing",
    [{"SMTP_HOST": "", "EMAIL_FROM": "a@x.test"}, {"SMTP_HOST": "smtp.x.test", "EMAIL_FROM": ""}],
)
def test_smtp_requires_host_and_sender(missing: dict[str, str]) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings(EMAIL_PROVIDER="smtp", SMTP_PASSWORD="pw-should-not-echo", **missing)
    # What get_settings() prints at startup: the messages, never the inputs.
    messages = " ".join(str(e["msg"]) for e in exc.value.errors())
    assert "EMAIL_PROVIDER=smtp requires" in messages
    assert "pw-should-not-echo" not in messages


@pytest.mark.parametrize(
    "bad",
    [
        "localhost:3000",
        "javascript:alert(1)",
        "ftp://files.example.test",
        "https://",
        "https://app.example.test/?next=//evil.test",
        "https://app.example.test/#frag",
        "https://user:pw@app.example.test",
    ],
)
def test_app_base_url_must_be_a_clean_http_origin(bad: str) -> None:
    with pytest.raises(ValidationError):
        _settings(APP_BASE_URL=bad)


def test_app_base_url_trailing_slash_is_trimmed() -> None:
    assert _settings(APP_BASE_URL=" https://app.example.test/ ").APP_BASE_URL == (
        "https://app.example.test"
    )


def test_production_requires_an_https_base_url() -> None:
    prod: dict[str, Any] = {
        "JWT_SECRET": secrets.token_hex(32),
        "APP_ENV": "production",
        "APP_DEBUG": False,
        "CORS_ALLOWED_ORIGINS": "https://app.example.test",
        "SEED_DEFAULT_PASSWORD": secrets.token_urlsafe(24),
    }
    with pytest.raises(ValidationError) as exc:
        _settings(APP_BASE_URL="http://app.example.test", **prod)
    assert "APP_BASE_URL" in str(exc.value)
    ok = _settings(APP_BASE_URL="https://app.example.test", **prod)
    assert deployment_secret_problems(ok) == []


def test_staging_refuses_a_non_https_base_url() -> None:
    """Review fix (intentional behaviour change): staging used to only warn;
    the spec requires https on staging too, so it is now a startup error."""
    with pytest.raises(ValidationError) as exc:
        _settings(APP_ENV="staging", APP_BASE_URL="http://localhost:3000")
    assert "APP_BASE_URL" in str(exc.value)
