"""Factory: the configured EmailProvider (EMAIL_PROVIDER = console | smtp | off)."""

from __future__ import annotations

from config.settings import get_settings
from services.email_provider.console import ConsoleEmailProvider, OffEmailProvider
from services.email_provider.interface import EmailProvider
from services.email_provider.smtp import SmtpEmailProvider
from services.exceptions import ProviderConfigurationError


def get_email_provider() -> EmailProvider:
    """Return the EmailProvider configured via the EMAIL_PROVIDER setting.

    Default: console (nothing delivered; one masked log line per message).
    Settings validation already refuses smtp without SMTP_HOST / EMAIL_FROM;
    the check here is a second guard for code that builds Settings by hand.
    """
    s = get_settings()
    mode = s.EMAIL_PROVIDER
    if mode == "console":
        return ConsoleEmailProvider()
    if mode == "off":
        return OffEmailProvider()
    if mode == "smtp":
        if not s.SMTP_HOST.strip() or not s.EMAIL_FROM.strip():
            raise ProviderConfigurationError("EMAIL_PROVIDER=smtp needs SMTP_HOST and EMAIL_FROM")
        return SmtpEmailProvider(
            host=s.SMTP_HOST.strip(),
            port=int(s.SMTP_PORT),
            sender=s.EMAIL_FROM.strip(),
            username=s.SMTP_USERNAME,
            password=s.SMTP_PASSWORD,
            use_tls=bool(s.SMTP_USE_TLS),
            timeout=float(s.SMTP_TIMEOUT_SECONDS),
        )
    raise ProviderConfigurationError(f"Unknown EMAIL_PROVIDER: {mode!r}")
