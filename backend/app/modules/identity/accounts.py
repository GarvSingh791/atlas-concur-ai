"""Account recovery — password reset / change, invitations, email verification.

Phase 12 follow-ups (docs/phase_logs/phase_12_followups_accounts.md).

One-time tokens (``auth_tokens``, migration 0024)
-------------------------------------------------
* 32 random bytes, URL-safe (``secrets.token_urlsafe``).  Only the SHA-256 hex
  digest is stored; the raw token exists in the emailed link (and, for an
  invitation without email delivery, once in the inviting admin's response).
  It is never logged, audited or put in an error message.
* Looked up by digest (unique index), then compared in constant time,
  checked for purpose, expiry, single use and that the account still belongs
  to the token's organisation.  Every failure is the same generic answer
  (400 ``INVALID_OR_EXPIRED_TOKEN``; the invite peek: a generic 404).
* Purpose-bound: a reset token can never accept an invitation or verify an
  email, and so on.  Issuing a new token of a purpose voids the user's earlier
  unused ones of that purpose; using one marks it used.

No account-existence oracle
---------------------------
``POST /auth/forgot-password`` always answers 202 with the same body.  Its
database and hashing work has one fixed shape whatever exists: ONE candidate
SELECT, a fixed number of token generations, ONE UPDATE (void earlier tokens)
and ONE INSERT ... SELECT whose VALUES list is padded with random ids that
match no user — so an unknown, inactive, invited or protected account costs
exactly the same statements as a real one.  The email itself is sent after the
response (a background task) and after the commit, so delivery latency is
never visible to the caller either.  Throttling runs before any of it
(backend/app/core/rate_limit.py).

Sessions
--------
A reset, a change and an invitation acceptance set ``users.password_changed_at``;
every access / refresh token carries a fingerprint of it, so tokens minted
before the change are refused (backend/app/core/security.py).  A password
change re-issues the caller's own session.

DEMO_MODE
---------
The shared demo accounts (backend/app/core/demo.py) never get a reset email,
cannot redeem a reset token, cannot change their password and get no
verification email.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.demo import (
    demo_mode,
    demo_org_name,
    guard_protected_account,
    is_protected_account,
    protected_demo_emails,
)
from backend.app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableEntityError,
)
from backend.app.core.security import hash_password, verify_password
from backend.app.modules.identity import admin as user_admin
from backend.app.modules.identity.password_policy import has_usable_password, password_problem
from backend.app.modules.identity.schemas import TokenResponse, UserInviteRequest
from backend.app.modules.identity.service import NO_ORG_LOGIN_CANDIDATES, issue_tokens
from config.settings import get_settings
from database.models.auth_token import AuthToken, AuthTokenPurpose
from database.models.identity import Organization, User, UserStatus, normalise_org_slug
from services.email_provider.interface import (
    PURPOSE_INVITE,
    PURPOSE_RESET,
    PURPOSE_VERIFY,
    EmailMessage,
)

# ---------------------------------------------------------------------------
# Errors and messages
# ---------------------------------------------------------------------------

INVALID_OR_EXPIRED_TOKEN = "INVALID_OR_EXPIRED_TOKEN"
INVALID_TOKEN_MESSAGE = "This link is invalid or has expired. Please request a new one."
INVITE_LINK_MESSAGE = "This invitation link is invalid or has expired."
NOT_INVITED = "NOT_INVITED"
INVALID_CURRENT_PASSWORD = "INVALID_CURRENT_PASSWORD"

FORGOT_PASSWORD_ACCEPTED = (
    "If an account matches that email, a password reset link is on its way. "
    "It expires soon, so use it promptly."
)
VERIFICATION_SENT = "We've sent a verification link to your email address."
ALREADY_VERIFIED = "Your email address is already verified."


class InvalidTokenError(AppError):
    """The ONE answer for every bad / expired / used / foreign token (400)."""

    status_code = 400
    code = INVALID_OR_EXPIRED_TOKEN

    def __init__(self) -> None:
        super().__init__(INVALID_TOKEN_MESSAGE)


class InviteLinkNotFoundError(NotFoundError):
    """The invite peek's generic 404 (any bad token)."""

    def __init__(self) -> None:
        super().__init__(INVITE_LINK_MESSAGE)


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

TOKEN_BYTES = 32
# token_urlsafe(32) is 43 characters; anything far outside is not ours.
_TOKEN_MIN_LEN = 32
_TOKEN_MAX_LEN = 128
_TOKEN_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def hash_token(raw: str) -> str:
    """SHA-256 hex digest — the only form of a token that is ever stored."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """``(raw, digest)`` for a fresh 32-byte URL-safe token."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, hash_token(raw)


def _plausible(raw: str) -> bool:
    return _TOKEN_MIN_LEN <= len(raw) <= _TOKEN_MAX_LEN and set(raw) <= _TOKEN_ALPHABET


def _now() -> datetime:
    return datetime.now(timezone.utc)


def requester_hash(ip: str) -> str:
    """Keyed hash of the requesting address (``auth_tokens.requested_ip_hash``)."""
    key = get_settings().JWT_SECRET.encode("utf-8")
    return hmac.new(key, f"ip:{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def _find_token(
    session: Session, raw: str, purpose: AuthTokenPurpose, *, lock: bool
) -> tuple[AuthToken, User] | None:
    """The live token row and its account, or None for ANY failure (one path).

    Checks: plausible shape, digest lookup, constant-time digest compare,
    purpose, not used, not expired, and the account still in the token's org.
    """
    if not _plausible(raw):
        return None
    digest = hash_token(raw)
    stmt = sa.select(AuthToken).where(AuthToken.token_hash == digest)
    if lock:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).first()
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None
    if row.purpose != purpose.value or row.used_at is not None or row.expires_at <= _now():
        return None
    user_stmt = sa.select(User).where(
        User.id == row.user_id, User.organization_id == row.organization_id
    )
    if lock:
        user_stmt = user_stmt.with_for_update()
    user = session.scalars(user_stmt).first()
    if user is None:
        return None
    return row, user


def _void_tokens(
    session: Session,
    *,
    user_ids: list[uuid.UUID],
    purpose: AuthTokenPurpose,
    now: datetime,
    keep: uuid.UUID | None = None,
) -> None:
    """Mark every unused token of ``purpose`` for ``user_ids`` used (ONE UPDATE)."""
    stmt = sa.update(AuthToken).where(
        AuthToken.user_id.in_(user_ids),
        AuthToken.purpose == purpose.value,
        AuthToken.used_at.is_(None),
    )
    if keep is not None:
        stmt = stmt.where(AuthToken.id != keep)
    session.execute(stmt.values(used_at=now).execution_options(synchronize_session=False))


def _issue(
    session: Session,
    *,
    user: User,
    purpose: AuthTokenPurpose,
    ttl: timedelta,
    created_by: uuid.UUID | None,
    ip_hash: str | None = None,
) -> tuple[str, datetime]:
    """Void the user's earlier tokens of ``purpose`` and add a fresh one."""
    now = _now()
    _void_tokens(session, user_ids=[user.id], purpose=purpose, now=now)
    raw, digest = new_token()
    expires = now + ttl
    session.add(
        AuthToken(
            organization_id=user.organization_id,
            user_id=user.id,
            purpose=purpose.value,
            token_hash=digest,
            expires_at=expires,
            created_by_id=created_by,
            requested_ip_hash=ip_hash,
        )
    )
    session.flush()
    return raw, expires


# ---------------------------------------------------------------------------
# Links and messages (links are built ONLY from APP_BASE_URL)
# ---------------------------------------------------------------------------

RESET_PATH = "/reset-password"
INVITE_PATH = "/accept-invite"
VERIFY_PATH = "/verify-email"


def build_link(path: str, raw_token: str) -> str:
    """``APP_BASE_URL + path + ?token=…`` — never from request headers / Host."""
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}{path}?{urlencode({'token': raw_token})}"


def _ttl_text(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} minutes"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _message(
    *, to: str, subject: str, purpose: str, greeting: str, lines: list[str], link: str, cta: str
) -> EmailMessage:
    text = "\n\n".join([greeting, *lines, f"{cta}: {link}", "— Atlas"])
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in lines)
    body = (
        f"<p>{html.escape(greeting)}</p>{paragraphs}"
        f'<p><a href="{html.escape(link, quote=True)}">{html.escape(cta)}</a></p>'
        "<p>— Atlas</p>"
    )
    return EmailMessage(to=to, subject=subject, text_body=text, html_body=body, purpose=purpose)


def reset_message(user: User, link: str) -> EmailMessage:
    ttl = _ttl_text(timedelta(minutes=get_settings().AUTH_RESET_LINK_TTL_MINUTES))
    return _message(
        to=user.email,
        subject="Reset your Atlas password",
        purpose=PURPOSE_RESET,
        greeting=f"Hi {user.name},",
        lines=[
            "Someone asked to reset the password of your Atlas account.",
            f"The link below works once and expires in {ttl}.",
            "If it wasn't you, ignore this email: your password stays the same.",
        ],
        link=link,
        cta="Choose a new password",
    )


def invite_message(user: User, *, org_name: str, inviter: str, link: str) -> EmailMessage:
    ttl = _ttl_text(timedelta(hours=get_settings().AUTH_INVITE_LINK_TTL_HOURS))
    return _message(
        to=user.email,
        subject=f"You're invited to Atlas ({org_name})",
        purpose=PURPOSE_INVITE,
        greeting=f"Hi {user.name},",
        lines=[
            f"{inviter} invited you to {org_name} on Atlas, the travel and expense app.",
            f"Choose your password to activate your account. The link expires in {ttl}.",
        ],
        link=link,
        cta="Accept the invitation",
    )


def verify_message(user: User, link: str) -> EmailMessage:
    ttl = _ttl_text(timedelta(hours=get_settings().AUTH_VERIFY_LINK_TTL_HOURS))
    return _message(
        to=user.email,
        subject="Verify your Atlas email address",
        purpose=PURPOSE_VERIFY,
        greeting=f"Hi {user.name},",
        lines=[f"Confirm this is your email address. The link expires in {ttl}."],
        link=link,
        cta="Verify my email",
    )


def mask_email(email: str) -> str:
    """``j***@example.com`` — the invitee recognises their address, nobody else learns it."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


# ---------------------------------------------------------------------------
# Password reset (anonymous)
# ---------------------------------------------------------------------------


def _protected_by_name(user: User, org_name: str) -> bool:
    """demo.py's provenance rule without a query (the org name is already joined)."""
    return (
        demo_mode()
        and (user.email or "").lower() in protected_demo_emails()
        and org_name == demo_org_name()
    )


def request_password_reset(
    session: Session, *, email: str, organization: str | None, ip_hash: str | None
) -> list[EmailMessage]:
    """Issue reset tokens for the matching ACTIVE, non-protected account(s).

    Returns the messages to send AFTER the commit (the caller schedules them
    after the response).  The statement / token work is identical whether or
    not anything matches (see the module docstring); nothing is audited here
    (an audit row would be one more statement only for real accounts — the
    ``auth_tokens`` row itself, with its time and requester hash, is the record).

    Without an organisation code the K oldest active accounts with the email
    are candidates (as at login); each gets its own org-bound link.
    """
    email_key = email.strip().lower()
    org_key = normalise_org_slug(organization)
    slots = 1 if org_key is not None else NO_ORG_LOGIN_CANDIDATES

    stmt = (
        sa.select(User, Organization.name)
        .join(Organization, Organization.id == User.organization_id)
        .where(sa.func.lower(User.email) == email_key, User.status == UserStatus.active)
    )
    if org_key is not None:
        stmt = stmt.where(Organization.slug == org_key)
    rows = session.execute(stmt.order_by(User.created_at.asc(), User.id.asc()).limit(slots)).all()
    eligible = [
        (user, org_name)
        for user, org_name in rows
        if not _protected_by_name(user, org_name) and has_usable_password(user.password_hash)
    ]

    # Fixed-shape work: `slots` tokens, one UPDATE, one INSERT ... SELECT.
    tokens = [new_token() for _ in range(slots)]
    ids = [user.id for user, _ in eligible]
    ids += [uuid.uuid4() for _ in range(slots - len(ids))]  # match no user
    now = _now()
    expires = now + timedelta(minutes=get_settings().AUTH_RESET_LINK_TTL_MINUTES)
    _void_tokens(session, user_ids=ids, purpose=AuthTokenPurpose.password_reset, now=now)
    slot_values = sa.values(
        sa.column("slot_user_id", PG_UUID(as_uuid=True)),
        sa.column("slot_token_hash", sa.Text()),
        name="reset_slots",
    ).data([(uid, digest) for uid, (_raw, digest) in zip(ids, tokens, strict=True)])
    source = sa.select(
        User.organization_id,
        User.id,
        sa.literal(AuthTokenPurpose.password_reset.value),
        slot_values.c.slot_token_hash,
        sa.literal(expires, sa.TIMESTAMP(timezone=True)),
        User.id,
        sa.literal(ip_hash, sa.Text()),
    ).join_from(User, slot_values, User.id == slot_values.c.slot_user_id)
    session.execute(
        sa.insert(AuthToken).from_select(
            [
                "organization_id",
                "user_id",
                "purpose",
                "token_hash",
                "expires_at",
                "created_by_id",
                "requested_ip_hash",
            ],
            source,
        )
    )
    session.commit()

    return [
        reset_message(user, build_link(RESET_PATH, raw))
        for (user, _org), (raw, _digest) in zip(eligible, tokens, strict=False)
    ]


def reset_password(session: Session, *, raw_token: str, new_password: str) -> None:
    """Redeem a reset token: new password, every old session revoked, audited."""
    found = _find_token(session, raw_token, AuthTokenPurpose.password_reset, lock=True)
    if found is None:
        raise InvalidTokenError()
    row, user = found
    if user.status != UserStatus.active or is_protected_account(session, user):
        raise InvalidTokenError()
    problem = password_problem(new_password, email=user.email)
    if problem is not None:
        raise UnprocessableEntityError(problem)
    now = _now()
    user.password_hash = hash_password(new_password)
    user.password_changed_at = now
    verified_now = user.email_verified_at is None
    if verified_now:
        user.email_verified_at = now  # the link reached this mailbox
    row.used_at = now
    _void_tokens(
        session, user_ids=[user.id], purpose=AuthTokenPurpose.password_reset, now=now, keep=row.id
    )
    session.flush()
    record_audit(
        session,
        organization_id=user.organization_id,
        action="user.password_reset",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        before_state=None,
        after_state={
            "password_changed": True,
            "sessions_revoked": True,
            "email_verified": verified_now or None,
        },
    )
    session.commit()


# ---------------------------------------------------------------------------
# Password change (signed in)
# ---------------------------------------------------------------------------


def change_password(
    session: Session, *, user: User, current_password: str, new_password: str
) -> TokenResponse:
    """Verify the current password, set the new one, revoke every other session.

    Returns a fresh token pair for the caller (their old tokens die with the rest).
    """
    guard_protected_account(session, admin=user, target=user, operation="change_password")
    if not verify_password(current_password, user.password_hash):
        raise BadRequestError("Your current password is incorrect.", code=INVALID_CURRENT_PASSWORD)
    if new_password == current_password:
        raise UnprocessableEntityError("Choose a password different from your current one.")
    problem = password_problem(new_password, email=user.email)
    if problem is not None:
        raise UnprocessableEntityError(problem)
    now = _now()
    user.password_hash = hash_password(new_password)
    user.password_changed_at = now
    # An outstanding reset link must not outlive a deliberate change.
    _void_tokens(session, user_ids=[user.id], purpose=AuthTokenPurpose.password_reset, now=now)
    session.flush()
    record_audit(
        session,
        organization_id=user.organization_id,
        action="user.password_changed",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        before_state=None,
        after_state={"password_changed": True, "sessions_revoked": True},
    )
    session.commit()
    return issue_tokens(user)


# ---------------------------------------------------------------------------
# Invitations (finance admin)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedInvite:
    user: User
    raw_token: str
    expires_at: datetime
    message: EmailMessage

    @property
    def link(self) -> str:
        return build_link(INVITE_PATH, self.raw_token)


def _org_name(session: Session, organization_id: uuid.UUID) -> str:
    return session.scalars(
        sa.select(Organization.name).where(Organization.id == organization_id)
    ).one()


def _issue_invite(session: Session, *, admin: User, user: User) -> IssuedInvite:
    raw, expires = _issue(
        session,
        user=user,
        purpose=AuthTokenPurpose.invite,
        ttl=timedelta(hours=get_settings().AUTH_INVITE_LINK_TTL_HOURS),
        created_by=admin.id,
    )
    message = invite_message(
        user,
        org_name=_org_name(session, user.organization_id),
        inviter=admin.name,
        link=build_link(INVITE_PATH, raw),
    )
    return IssuedInvite(user=user, raw_token=raw, expires_at=expires, message=message)


def _is_placeholder(user: User) -> bool:
    """An invited account that was never activated (no usable password yet)."""
    return user.status == UserStatus.inactive and not has_usable_password(user.password_hash)


def invite_user(session: Session, *, admin: User, body: UserInviteRequest) -> IssuedInvite:
    """Create the account (inactive, unusable password) and its invitation.

    Same validations and governance as POST /users (email unique per
    organization with the same audited 409 EMAIL_TAKEN, role, manager, grade).
    """
    user = user_admin.build_new_user(session, admin=admin, body=body, password=None)
    issued = _issue_invite(session, admin=admin, user=user)
    record_audit(
        session,
        organization_id=admin.organization_id,
        action="user.invited",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        before_state=None,
        after_state={
            **user_admin.snapshot(user),
            "invite_expires_at": issued.expires_at.isoformat(),
        },
    )
    session.commit()
    return issued


def _load_placeholder(session: Session, *, admin: User, user_id: uuid.UUID, operation: str) -> User:
    user = user_admin.load_org_user(session, admin=admin, user_id=user_id, for_update=True)
    guard_protected_account(session, admin=admin, target=user, operation=operation)
    if not _is_placeholder(user):
        raise ConflictError(
            "This user has already activated their account, so there is no invitation to "
            "manage.",
            code=NOT_INVITED,
        )
    return user


def resend_invite(session: Session, *, admin: User, user_id: uuid.UUID) -> IssuedInvite:
    """A fresh invitation for a not-yet-activated account (earlier links die)."""
    user = _load_placeholder(session, admin=admin, user_id=user_id, operation="invite_resend")
    issued = _issue_invite(session, admin=admin, user=user)
    record_audit(
        session,
        organization_id=admin.organization_id,
        action="user.invite_resent",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        before_state=None,
        after_state={"invite_expires_at": issued.expires_at.isoformat()},
    )
    session.commit()
    return issued


def revoke_invite(session: Session, *, admin: User, user_id: uuid.UUID) -> User:
    """Void every open invitation of the account; it stays deactivated."""
    user = _load_placeholder(session, admin=admin, user_id=user_id, operation="invite_revoke")
    now = _now()
    _void_tokens(session, user_ids=[user.id], purpose=AuthTokenPurpose.invite, now=now)
    user.status = UserStatus.inactive
    session.flush()
    record_audit(
        session,
        organization_id=admin.organization_id,
        action="user.invite_revoked",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        before_state=None,
        after_state={"status": UserStatus.inactive.value, "invite_revoked": True},
    )
    session.commit()
    return user


@dataclass(frozen=True)
class InvitePeek:
    organization_name: str
    email_masked: str
    name: str
    expires_at: datetime


def peek_invite(session: Session, *, raw_token: str) -> InvitePeek:
    """What the accept page shows; a generic 404 for anything but a live invite."""
    found = _find_token(session, raw_token, AuthTokenPurpose.invite, lock=False)
    if found is None or not _is_placeholder(found[1]):
        raise InviteLinkNotFoundError()
    row, user = found
    return InvitePeek(
        organization_name=_org_name(session, row.organization_id),
        email_masked=mask_email(user.email),
        name=user.name,
        expires_at=row.expires_at,
    )


def accept_invite(session: Session, *, raw_token: str, password: str, name: str | None) -> None:
    """Set the password, activate the account and mark the email verified.

    Issues no session: the invitee signs in on the login page afterwards.
    """
    found = _find_token(session, raw_token, AuthTokenPurpose.invite, lock=True)
    if found is None:
        raise InvalidTokenError()
    row, user = found
    if not _is_placeholder(user) or is_protected_account(session, user):
        raise InvalidTokenError()
    problem = password_problem(password, email=user.email)
    if problem is not None:
        raise UnprocessableEntityError(problem)
    before = {"status": user.status.value, "name": user.name}
    if name is not None:
        cleaned = " ".join(name.split())
        if not cleaned:
            raise UnprocessableEntityError("Full name is required.")
        user.name = cleaned
    now = _now()
    user.password_hash = hash_password(password)
    user.password_changed_at = now
    user.email_verified_at = now
    user.status = UserStatus.active
    row.used_at = now
    _void_tokens(session, user_ids=[user.id], purpose=AuthTokenPurpose.invite, now=now, keep=row.id)
    session.flush()
    record_audit(
        session,
        organization_id=user.organization_id,
        action="user.invite_accepted",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        before_state=before,
        after_state={"status": user.status.value, "name": user.name, "email_verified": True},
    )
    session.commit()


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def send_verification(session: Session, *, user: User, ip_hash: str | None) -> EmailMessage | None:
    """A verification link for the signed-in user (None when already verified)."""
    guard_protected_account(session, admin=user, target=user, operation="send_verification")
    if user.email_verified_at is not None:
        return None
    raw, expires = _issue(
        session,
        user=user,
        purpose=AuthTokenPurpose.email_verify,
        ttl=timedelta(hours=get_settings().AUTH_VERIFY_LINK_TTL_HOURS),
        created_by=user.id,
        ip_hash=ip_hash,
    )
    record_audit(
        session,
        organization_id=user.organization_id,
        action="user.email_verification_requested",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        before_state=None,
        after_state={"expires_at": expires.isoformat()},
    )
    session.commit()
    return verify_message(user, build_link(VERIFY_PATH, raw))


def verify_email(session: Session, *, raw_token: str) -> None:
    """Redeem a verification token (signed in or not: the link proves the mailbox)."""
    found = _find_token(session, raw_token, AuthTokenPurpose.email_verify, lock=True)
    if found is None:
        raise InvalidTokenError()
    row, user = found
    if user.status != UserStatus.active:
        raise InvalidTokenError()
    now = _now()
    already = user.email_verified_at is not None
    if not already:
        user.email_verified_at = now
    row.used_at = now
    session.flush()
    record_audit(
        session,
        organization_id=user.organization_id,
        action="user.email_verified",
        entity_type="user",
        entity_id=user.id,
        actor_id=user.id,
        before_state=None,
        after_state={"email_verified": True},
    )
    session.commit()
