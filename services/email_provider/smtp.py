"""SMTP email provider — stdlib ``smtplib`` + ``ssl``, no new dependency.

* TLS: with ``SMTP_USE_TLS`` (default) port 465 uses implicit TLS
  (``SMTP_SSL``), any other port upgrades with STARTTLS; both verify the
  server certificate against the system trust store
  (``ssl.create_default_context``).  ``SMTP_USE_TLS=false`` is plain SMTP for
  local relays / test sinks only.
* Timeouts: every socket operation is bounded by ``SMTP_TIMEOUT_SECONDS``.
* Bodies: multipart/alternative, plain text first, then HTML.
* Failures never propagate: the caller gets ``False`` and one WARNING with the
  exception CLASS and the masked recipient — never the server's reply text
  (it can echo the address), the link, the token or the credentials.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid

from services.email_provider.interface import EmailMessage, EmailProvider, mask_address

logger = logging.getLogger(__name__)

IMPLICIT_TLS_PORT = 465


class SmtpEmailProvider(EmailProvider):
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        use_tls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout = timeout

    def __repr__(self) -> str:  # never show credentials
        return f"SmtpEmailProvider(host={self._host!r}, port={self._port}, tls={self._use_tls})"

    @property
    def delivers(self) -> bool:
        return True

    def build(self, message: EmailMessage) -> MimeMessage:
        mime = MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime["Date"] = formatdate(localtime=False)
        mime["Message-ID"] = make_msgid()
        mime["Auto-Submitted"] = "auto-generated"
        mime.set_content(message.text_body)
        mime.add_alternative(message.html_body, subtype="html")
        return mime

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self._use_tls and self._port == IMPLICIT_TLS_PORT:
            return smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=context)
        client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        if self._use_tls:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        return client

    def send(self, message: EmailMessage) -> bool:
        try:
            mime = self.build(message)
            client = self._connect()
            try:
                if self._username:
                    client.login(self._username, self._password)
                client.send_message(mime)
            finally:
                try:
                    client.quit()
                except Exception:  # noqa: BLE001 - closing a broken session
                    client.close()
        except Exception as exc:  # noqa: BLE001 - delivery must never break the API
            logger.warning(
                "email delivery failed: provider=smtp purpose=%s to=%s error=%s",
                message.purpose,
                mask_address(message.to),
                type(exc).__name__,
            )
            return False
        logger.info(
            "email sent: provider=smtp purpose=%s to=%s",
            message.purpose,
            mask_address(message.to),
        )
        return True
