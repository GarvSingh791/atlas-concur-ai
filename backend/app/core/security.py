"""Auth primitives: password hashing, JWT token creation/verification,
and the get_current_user FastAPI dependency.

Design decisions:
  - Hashing: bcrypt directly (passlib 1.7.x is incompatible with bcrypt>=4.0).
    Rounds from Settings.PASSWORD_HASH_ROUNDS.
  - JWT: python-jose with HS256 (algorithm from Settings).
  - Token claims: sub (user id), org (organization_id), type ("access"|"refresh"),
    exp (from configured lifetimes), iat, and — once the account's password has
    been changed through the app (reset / change / invite acceptance) — ``pwv``,
    a keyed fingerprint of ``users.password_changed_at``.  Every token check
    compares it with the CURRENT fingerprint, so changing the password revokes
    every access and refresh token minted before (fail closed: a token without
    the claim is valid only while the account has no password_changed_at).
  - get_current_user: AUTHENTICATES ONLY — no role/permission check (P2-T4).
    Rejects refresh tokens used where an access token is required.
  - Missing-user path still calls verify_password against a dummy hash to keep
    the timing behaviour indistinguishable from a wrong-password path (no user
    enumeration).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import bcrypt as _bcrypt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.core.errors import UnauthorizedError
from config.settings import get_settings

_settings = get_settings()

# ---------------------------------------------------------------------------
# Password hashing (bcrypt direct — avoids passlib/bcrypt4+ incompatibility)
# ---------------------------------------------------------------------------

_ROUNDS: int = _settings.PASSWORD_HASH_ROUNDS


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of *plaintext*."""
    salt = _bcrypt.gensalt(rounds=_ROUNDS)
    return _bcrypt.hashpw(plaintext.encode(), salt).decode()


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches *hashed*.  Never raises.

    A stored value that is not a bcrypt hash (an invitation placeholder
    ``!unusable:…``, a malformed row) makes bcrypt raise in microseconds; that
    fast failure would be a timing oracle.  Phase 12 review fix: the failure
    path still does one full-cost check against ``DUMMY_HASH`` and returns False.
    """
    try:
        return _bcrypt.checkpw(plaintext.encode(), hashed.encode())
    except Exception:
        try:
            _bcrypt.checkpw(plaintext.encode(), DUMMY_HASH.encode())
        except Exception:
            pass
        return False


# Precomputed dummy hash used for constant-time behaviour on unknown emails.
# Non-private so the identity service can import it for the missing-user path.
DUMMY_HASH: str = hash_password("__atlas_dummy__")

# ---------------------------------------------------------------------------
# JWT token creation
# ---------------------------------------------------------------------------

_TOKEN_TYPE_ACCESS = "access"
_TOKEN_TYPE_REFRESH = "refresh"


# Claim carrying the password fingerprint (see the module docstring).
PASSWORD_VERSION_CLAIM = "pwv"


def password_version(user_id: uuid.UUID, changed_at: datetime | None) -> str | None:
    """Keyed fingerprint of ``users.password_changed_at`` (None while never changed).

    Microsecond epoch (timezone-independent) under HMAC-SHA256 with the JWT key,
    so the token does not reveal when the password changed.
    """
    if changed_at is None:
        return None
    micros = round(changed_at.timestamp() * 1_000_000)
    message = f"{user_id}:{micros}".encode()
    key = _settings.JWT_SECRET.encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:32]


def ensure_token_current(payload: dict[str, Any], user: Any) -> None:
    """Refuse a token minted before the account's last password change (401)."""
    expected = password_version(user.id, getattr(user, "password_changed_at", None))
    claim = payload.get(PASSWORD_VERSION_CLAIM)
    if expected is None and claim is None:
        return
    if (
        not isinstance(claim, str)
        or expected is None
        or not hmac.compare_digest(claim.encode(), expected.encode())
    ):
        raise UnauthorizedError("Invalid or expired token.")


def _make_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    token_type: str,
    expires_delta: timedelta,
    password_changed_at: datetime | None = None,
) -> str:
    now = datetime.now(tz=timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "org": str(organization_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    version = password_version(user_id, password_changed_at)
    if version is not None:
        payload[PASSWORD_VERSION_CLAIM] = version
    return jwt.encode(payload, _settings.JWT_SECRET, algorithm=_settings.JWT_ALGORITHM)


def create_access_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    password_changed_at: datetime | None = None,
) -> str:
    """Issue a short-lived access token (``password_changed_at``: the user's, if any)."""
    return _make_token(
        user_id,
        organization_id,
        _TOKEN_TYPE_ACCESS,
        timedelta(minutes=_settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        password_changed_at,
    )


def create_refresh_token(
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    *,
    password_changed_at: datetime | None = None,
) -> str:
    """Issue a longer-lived refresh token (``password_changed_at``: the user's, if any)."""
    return _make_token(
        user_id,
        organization_id,
        _TOKEN_TYPE_REFRESH,
        timedelta(days=_settings.REFRESH_TOKEN_EXPIRE_DAYS),
        password_changed_at,
    )


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------


def decode_token(token: str, *, expected_type: str) -> dict[str, Any]:
    """Decode and validate *token*.

    Raises UnauthorizedError for any failure: invalid signature, expired,
    wrong type, or missing required claims.
    """
    try:
        payload = jwt.decode(
            token,
            _settings.JWT_SECRET,
            algorithms=[_settings.JWT_ALGORITHM],
        )
    except JWTError as exc:
        raise UnauthorizedError("Invalid or expired token.") from exc

    if payload.get("type") != expected_type:
        raise UnauthorizedError("Invalid token type.")

    if not payload.get("sub") or not payload.get("org"):
        raise UnauthorizedError("Token is missing required claims.")

    return payload


# ---------------------------------------------------------------------------
# FastAPI dependency: get_current_user
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> Any:
    """FastAPI dependency — authenticate the caller and return the User ORM object.

    Rejects: missing/malformed bearer token, invalid/expired token, refresh
    tokens used where an access token is required, unknown user, inactive user.

    Does NOT check roles or permissions (that is P2-T4).
    """
    from database.models.identity import User, UserStatus  # noqa: PLC0415

    if not credentials:
        raise UnauthorizedError("Authentication required.")

    payload = decode_token(credentials.credentials, expected_type=_TOKEN_TYPE_ACCESS)

    try:
        user_id = uuid.UUID(payload["sub"])
        organization_id = uuid.UUID(payload["org"])
    except (ValueError, KeyError) as exc:
        raise UnauthorizedError("Invalid token claims.") from exc

    user = (
        db.query(User).filter(User.id == user_id, User.organization_id == organization_id).first()
    )

    if user is None:
        raise UnauthorizedError("User not found.")

    if user.status != UserStatus.active:
        raise UnauthorizedError("User account is inactive.")

    # Phase 12 follow-ups: a token minted before the last password change is dead.
    ensure_token_current(payload, user)

    return user
