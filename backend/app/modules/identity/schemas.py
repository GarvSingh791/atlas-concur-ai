"""Pydantic request/response schemas for the identity module.

password_hash is NEVER included in any response schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from database.models.identity import RoleKey, UserStatus


class LoginRequest(BaseModel):
    email: str
    password: str
    # Optional organization login code (organizations.slug, case-insensitive).
    # Needed only when the same email exists in more than one organization.
    organization: str | None = Field(default=None, max_length=100)


class OrganizationResponse(BaseModel):
    """GET /organization — the caller's own organization and its login code
    (non-secret; finance admins share it with their users)."""

    id: uuid.UUID
    name: str
    login_code: str


class TokenResponse(BaseModel):
    """Returned by POST /auth/login."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    """Returned by POST /auth/refresh."""

    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    """Returned by GET /me.  Never includes password_hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: RoleKey
    department: str | None
    organization_id: uuid.UUID
    # Phase 12 follow-ups: NULL = email not verified (a banner, never a block).
    email_verified_at: datetime | None = None
    # Phase 11 (DEMO_MODE): a shared demo account — no password change, no
    # verification email (set by the /me route; the server enforces it).
    demo_protected: bool = False


class UserResponse(BaseModel):
    """Returned by GET /users and GET /users/{id}.  Never includes password_hash."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: str
    role: RoleKey
    department: str | None
    manager_id: uuid.UUID | None
    status: UserStatus
    organization_id: uuid.UUID
    created_at: datetime
    # Product gap 3: the manager's display name for the admin table (batched
    # lookup in the service; None when there is no manager).
    manager_name: str | None = None
    # AIR-04 (known limits): the traveller grade code (config/constants/grades.py);
    # None = not recorded.  Set by a finance admin only.
    grade: str | None = None
    # Phase 11 (DEMO_MODE): a shared demo account no one may edit or deactivate —
    # a UI hint only; the server enforces it (backend/app/core/demo.py).
    demo_protected: bool = False
    # Phase 12 follow-ups (account recovery).  ``email_verified_at`` NULL = not
    # verified.  ``invite_pending``: an invited account that has not accepted yet
    # (it stays inactive until then); ``invite_expires_at`` is that invitation's
    # expiry (in the past = expired, resend it).
    email_verified_at: datetime | None = None
    invite_pending: bool = False
    invite_expires_at: datetime | None = None
    # Invited and never activated (no password yet), whether or not an
    # invitation is still open: resend it rather than reactivate.
    awaiting_activation: bool = False


# ---------------------------------------------------------------------------
# User administration (product gap 3) — finance_admin only.
# ---------------------------------------------------------------------------
#
# Fields are deliberately loose (plain str) so the service can return a
# specific, field-named 422 message (the generic request-validation envelope
# says only "Request validation failed.").  Unknown fields are refused, so a
# client can never smuggle ``status``/``password_hash``/``organization_id``
# through these bodies.


class UserCreateRequest(BaseModel):
    """POST /users.  ``password`` is the admin-set initial password: validated,
    hashed, and never logged, audited or echoed back."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    name: str = Field(max_length=200)
    role: str
    manager_id: uuid.UUID | None = None
    department: str | None = Field(default=None, max_length=200)
    password: str = Field(max_length=1024)
    # AIR-04: a ladder code (or title); omitted / null = not recorded.
    grade: str | None = Field(default=None, max_length=64)


class UserUpdateRequest(BaseModel):
    """PATCH /users/{id} — only the fields present in the body change.

    ``manager_id: null`` explicitly clears the manager; omitting it keeps it.
    Email, status and password are not editable here (status has its own
    deactivate / reactivate endpoints).
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    role: str | None = None
    manager_id: uuid.UUID | None = None
    department: str | None = Field(default=None, max_length=200)
    # AIR-04: ``grade: null`` clears it (unknown); omitting it keeps it.  An admin can
    # never change their OWN grade (segregation of duties).
    grade: str | None = Field(default=None, max_length=64)


class GradeResponse(BaseModel):
    """GET /grades — one rung of the traveller grade ladder (most senior first)."""

    code: str
    title: str
    short: str
    rank: int
    # Clause ids of the TEP-FY26 entitlements this grade holds (e.g. AIR-04).
    entitlements: list[str]


# ---------------------------------------------------------------------------
# Account recovery (phase 12 follow-ups): reset, change, invite, verification.
# ---------------------------------------------------------------------------
#
# Tokens travel ONLY in JSON bodies on mutating endpoints (never a query string);
# the invite peek is the one read that takes it in the path (and every logged
# path is redacted, backend/app/core/errors.py).  Passwords are never echoed.

_TOKEN_MAX = 256
_PASSWORD_MAX = 1024


class AcceptedResponse(BaseModel):
    """202 body — deliberately the same whatever happened behind it."""

    message: str


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    organization: str | None = Field(default=None, max_length=100)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=_TOKEN_MAX)
    new_password: str = Field(max_length=_PASSWORD_MAX)


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(max_length=_PASSWORD_MAX)
    new_password: str = Field(max_length=_PASSWORD_MAX)


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=_TOKEN_MAX)


class AcceptInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=_TOKEN_MAX)
    password: str = Field(max_length=_PASSWORD_MAX)
    # Optional: the invitee may correct the name the admin typed.
    name: str | None = Field(default=None, max_length=200)


class InvitePeekResponse(BaseModel):
    """GET /auth/invite/{token} — what the accept page shows (masked email)."""

    organization_name: str
    email_masked: str
    name: str
    expires_at: datetime


class UserInviteRequest(BaseModel):
    """POST /users/invite — the fields of POST /users without a password."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    name: str = Field(max_length=200)
    role: str
    manager_id: uuid.UUID | None = None
    department: str | None = Field(default=None, max_length=200)
    grade: str | None = Field(default=None, max_length=64)


class InviteResponse(BaseModel):
    """POST /users/invite and /users/{id}/invite/resend.

    ``invite_url`` is present ONLY when the server cannot deliver email
    (EMAIL_PROVIDER console / off) so the inviting admin can hand the link over;
    it is shown once and never stored, logged or audited.
    """

    user: UserResponse
    invite_url: str | None = None
    invite_expires_at: datetime
    email_provider: str
