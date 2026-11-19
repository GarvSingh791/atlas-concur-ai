"""Console and off email providers — nothing is delivered.

``console`` (the default) logs ONE line per message so an operator can see that
a reset / invite / verification email WOULD have been sent:

    email not delivered: provider=console purpose=reset to=a***@d***.com

It never logs the link, the token, the subject's variable parts or the full
recipient address: a log reader must not be able to take over an account.
``off`` drops messages silently (a per-message line would be noise).
"""

from __future__ import annotations

import logging

from services.email_provider.interface import EmailMessage, EmailProvider, mask_address

logger = logging.getLogger(__name__)


class ConsoleEmailProvider(EmailProvider):
    name = "console"

    def send(self, message: EmailMessage) -> bool:
        logger.info(
            "email not delivered: provider=console purpose=%s to=%s",
            message.purpose,
            mask_address(message.to),
        )
        return True


class OffEmailProvider(EmailProvider):
    name = "off"

    def send(self, message: EmailMessage) -> bool:
        return True
