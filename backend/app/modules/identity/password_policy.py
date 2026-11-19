"""Initial-password strength rules for admin-created accounts (product gap 3).

The codebase had no password policy (passwords were only ever seeded), so the
rules live here, next to the identity module that consumes them:

  * 12 to 72 characters — 72 *bytes* is bcrypt's hard input limit (anything
    beyond is silently ignored by bcrypt, so it is refused rather than truncated);
  * at least three of: lowercase, uppercase, digit, symbol;
  * must not contain the account's email local part (case-insensitive, when that
    part is 3+ characters long).

Messages are safe to return verbatim (they never echo the password).
"""

from __future__ import annotations

MIN_LENGTH = 12
MAX_BYTES = 72  # bcrypt input limit
MIN_CHARACTER_CLASSES = 3


def password_problem(password: str, *, email: str | None = None) -> str | None:
    """Return a human-readable reason the password is too weak, or None if it passes."""
    if len(password) < MIN_LENGTH:
        return f"Password must be at least {MIN_LENGTH} characters long."
    if len(password.encode("utf-8")) > MAX_BYTES:
        return f"Password must be at most {MAX_BYTES} bytes long."
    if password.strip() != password:
        return "Password must not start or end with whitespace."
    classes = sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )
    if classes < MIN_CHARACTER_CLASSES:
        return (
            "Password must mix at least three of: lowercase letters, uppercase letters, "
            "digits and symbols."
        )
    if email:
        local = email.split("@", 1)[0].strip().lower()
        if len(local) >= 3 and local in password.lower():
            return "Password must not contain the email address."
    return None


# ---------------------------------------------------------------------------
# Unusable password (phase 12 follow-ups: invitations)
# ---------------------------------------------------------------------------
#
# An invited account exists before its owner has chosen a password.  Its
# ``password_hash`` is this prefix plus random hex: never a valid bcrypt hash,
# so ``verify_password`` is always False for it (no password can match), and the
# prefix marks the row as "never activated" for the invitation endpoints and the
# reactivation guard.

UNUSABLE_PASSWORD_PREFIX = "!unusable:"


def unusable_password_hash() -> str:
    import secrets  # noqa: PLC0415

    return UNUSABLE_PASSWORD_PREFIX + secrets.token_hex(16)


def has_usable_password(password_hash: str | None) -> bool:
    return bool(password_hash) and not str(password_hash).startswith(UNUSABLE_PASSWORD_PREFIX)
