"""EmailProvider interface and the message type (phase 12 follow-ups: accounts).

Transactional account email only: password reset, invitation, email
verification.  A message carries a ``purpose`` (a short, non-secret label used
in logs) and the recipient; the bodies carry the one-time link and are NEVER
logged by any provider.

Contract every adapter honours:
  * ``send`` never raises to the caller for a delivery failure — it returns
    ``False`` and logs the failure WITHOUT the recipient, link, token or any
    credential.  The API layer answers the client the same way either way
    (a delivery problem must never become an account-existence oracle).
  * Nothing secret is ever logged: recipients appear masked
    (:func:`mask_address`), links / tokens / bodies never.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# Purposes (log labels; also the ``purpose=`` value of the console line).
PURPOSE_RESET = "reset"
PURPOSE_INVITE = "invite"
PURPOSE_VERIFY = "verify"


@dataclass(frozen=True)
class EmailMessage:
    """One outgoing message.  ``repr`` hides the recipient and both bodies."""

    to: str = field(repr=False)
    subject: str
    text_body: str = field(repr=False)
    html_body: str = field(repr=False)
    purpose: str = "account"


def mask_address(address: str) -> str:
    """``alice@example.com`` -> ``a***@e***.com``.  Never the full address.

    The first character of the local part, the first character of the domain
    label and the top-level domain only; anything unparseable -> ``***``.
    """
    local, sep, domain = (address or "").strip().partition("@")
    if not sep or not local or not domain:
        return "***"
    host, dot, tld = domain.rpartition(".")
    if not dot or not host or not tld:
        return f"{local[:1]}***@***"
    return f"{local[:1]}***@{host[:1]}***.{tld}"


class EmailProvider(ABC):
    """Send transactional email.  ``name`` is the configured EMAIL_PROVIDER value."""

    name: str = "abstract"

    @property
    def delivers(self) -> bool:
        """True when a message really leaves the process (smtp)."""
        return False

    @abstractmethod
    def send(self, message: EmailMessage) -> bool:
        """Deliver ``message``; True on success, False on a (logged) failure."""
