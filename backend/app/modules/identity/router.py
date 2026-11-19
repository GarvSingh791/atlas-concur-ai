"""Identity module router — auth + user-management endpoints.

Mounted at the API v1 root (no /identity prefix) so paths match the design
docs literally:
  POST /api/v1/auth/login
  POST /api/v1/auth/refresh
  GET  /api/v1/me
  GET  /api/v1/organization                   (finance_admin — login code, read-only)
  GET  /api/v1/users
  GET  /api/v1/users/{user_id}
  POST  /api/v1/users                         (finance_admin — product gap 3)
  PATCH /api/v1/users/{user_id}               (finance_admin)
  POST  /api/v1/users/{user_id}/deactivate    (finance_admin)
  POST  /api/v1/users/{user_id}/reactivate    (finance_admin)
  GET   /api/v1/grades                        (users.read — traveller grade ladder)

Account recovery (phase 12 follow-ups; backend/app/modules/identity/accounts.py):
  POST /api/v1/auth/forgot-password           (public — always 202, same body)
  POST /api/v1/auth/reset-password            (public — token in the body)
  POST /api/v1/auth/change-password           (signed in)
  POST /api/v1/auth/send-verification         (signed in)
  POST /api/v1/auth/verify-email              (public — token in the body)
  GET  /api/v1/auth/invite/{token}            (public — invite peek, generic 404)
  POST /api/v1/auth/accept-invite             (public — token in the body)
  POST /api/v1/users/invite                   (finance_admin)
  POST /api/v1/users/{user_id}/invite/resend  (finance_admin)
  POST /api/v1/users/{user_id}/invite/revoke  (finance_admin)

Emails are sent by a background task AFTER the response and after the commit
(no email for a rolled-back transaction; delivery latency is never visible).
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from backend.app.core.config import get_settings
from backend.app.core.database import get_db
from backend.app.core.demo import is_protected_account
from backend.app.core.errors import ForbiddenError
from backend.app.core.pagination import Page, PaginationParams, paginated
from backend.app.core.rate_limit import client_ip, guard_forgot_password, guard_login
from backend.app.core.rbac import require_permission
from backend.app.core.security import get_current_user
from backend.app.modules.identity import accounts, admin, schemas, service
from config.constants import grades as grade_ladder
from config.constants.permissions import USERS_READ
from database.models.identity import RoleKey, User, UserStatus
from services.email_provider.factory import get_email_provider
from services.email_provider.interface import EmailMessage, EmailProvider, mask_address

router = APIRouter(tags=["identity"])
logger = logging.getLogger(__name__)

# Query-string keys consumed by pagination/sort; excluded from filter parsing.
_PAGINATION_KEYS = {"page", "size", "sort"}

_require_users_read = require_permission(USERS_READ)


def require_user_admin(
    current_user: Annotated[Any, Depends(_require_users_read)],
) -> User:
    """User administration gate (product gap 3): ``users.read`` AND the
    finance_admin role — both must hold (deny-by-default; 403 otherwise).

    No new permission key is introduced: a key would need an RBAC data migration
    on every live database before the endpoints could work there.
    """
    user: User = current_user
    if user.role != RoleKey.finance_admin:
        raise ForbiddenError("You do not have permission to perform this action.")
    return user


@router.post("/auth/login", response_model=schemas.TokenResponse)
def login(
    request: Request,
    body: schemas.LoginRequest,
    db: Annotated[Session, Depends(get_db)],
) -> schemas.TokenResponse:
    """Authenticate with email + password (+ optional organization login code).

    Phase 11: guarded by the per-(IP, email) window and failed-login backoff
    (backend/app/core/rate_limit.py); the per-IP window runs in the middleware.
    """
    return guard_login(
        request,
        body.email,
        lambda: service.login(
            db, email=body.email, password=body.password, organization=body.organization
        ),
        organization=body.organization,
    )


@router.post("/auth/refresh", response_model=schemas.AccessTokenResponse)
def refresh(
    body: schemas.RefreshRequest,
    db: Annotated[Session, Depends(get_db)],
) -> schemas.AccessTokenResponse:
    """Exchange a valid refresh token for a new access token."""
    return service.refresh_access_token(db, refresh_token=body.refresh_token)


@router.get("/me", response_model=schemas.MeResponse)
def me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.MeResponse:
    """Return the authenticated user's profile.  Never includes password_hash."""
    out = schemas.MeResponse.model_validate(current_user)
    out.demo_protected = is_protected_account(db, current_user)
    return out


@router.get("/organization", response_model=schemas.OrganizationResponse)
def get_organization(
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.OrganizationResponse:
    """The caller's organization and its login code (finance admins, read-only)."""
    return service.get_organization(db, current_user)


@router.get("/users", response_model=Page[schemas.UserResponse])
def list_users(
    request: Request,
    current_user: Annotated[User, Depends(require_permission(USERS_READ))],
    db: Annotated[Session, Depends(get_db)],
    pagination: Annotated[PaginationParams, Depends(paginated)],
    sort: str | None = Query(None, description="Sort spec, e.g. 'name:asc' or '-created_at'."),
) -> Page[schemas.UserResponse]:
    """List users in the caller's organization (requires users.read permission).

    Supports pagination (?page, ?size), filtering (?role, ?status, ?email,
    ?name, ?department), and sorting (?sort=field:asc|desc or ?sort=-field).
    Unknown filter or sort fields are rejected with 400.
    """
    raw_filters = {k: v for k, v in request.query_params.items() if k not in _PAGINATION_KEYS}
    return service.list_users(db, current_user, pagination, raw_filters, sort)


@router.get("/grades", response_model=list[schemas.GradeResponse])
def list_grades(
    current_user: Annotated[User, Depends(require_permission(USERS_READ))],
) -> list[schemas.GradeResponse]:
    """The traveller grade ladder, most senior first (AIR-04), with the TEP-FY26
    entitlement clauses each grade holds.  Static data; no tenant rows are read."""
    return [
        schemas.GradeResponse(
            code=g.code,
            title=g.title,
            short=g.short,
            rank=g.rank,
            entitlements=[
                e.clause_id
                for e in grade_ladder.ENTITLEMENTS
                if grade_ladder.at_or_above(g.code, e.min_grade)
            ],
        )
        for g in grade_ladder.GRADES
    ]


@router.get("/users/{user_id}", response_model=schemas.UserResponse)
def get_user(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_permission(USERS_READ))],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Fetch a single user by ID (requires users.read permission).

    Returns 404 for both out-of-scope and nonexistent IDs — existence of
    records outside the caller's organization is never leaked.
    """
    return service.get_user(db, current_user, user_id)


# ---------------------------------------------------------------------------
# User administration (product gap 3) — finance_admin only, org-scoped, audited.
# ---------------------------------------------------------------------------


@router.post("/users", response_model=schemas.UserResponse, status_code=201)
def create_user(
    body: schemas.UserCreateRequest,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Create a user in the admin's organization with an admin-set initial password.

    The password is strength-checked, hashed and never logged, audited or echoed.
    """
    return admin.create_user(db, admin=current_user, body=body)


@router.patch("/users/{user_id}", response_model=schemas.UserResponse)
def update_user(
    user_id: uuid.UUID,
    body: schemas.UserUpdateRequest,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Edit a user's full name, role, manager or department."""
    return admin.update_user(db, admin=current_user, user_id=user_id, body=body)


@router.post("/users/{user_id}/deactivate", response_model=schemas.UserResponse)
def deactivate_user(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Deactivate a user: they can no longer log in and existing tokens stop working."""
    return admin.set_user_status(
        db, admin=current_user, user_id=user_id, status=UserStatus.inactive
    )


@router.post("/users/{user_id}/reactivate", response_model=schemas.UserResponse)
def reactivate_user(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Reactivate a previously deactivated user."""
    return admin.set_user_status(db, admin=current_user, user_id=user_id, status=UserStatus.active)


# ---------------------------------------------------------------------------
# Account recovery (phase 12 follow-ups)
# ---------------------------------------------------------------------------

Mailer = Annotated[EmailProvider, Depends(get_email_provider)]


def _deliver(mailer: EmailProvider, message: EmailMessage) -> None:
    """Background task: send one message; never raises (nothing to answer anymore)."""
    try:
        mailer.send(message)
    except Exception as exc:  # noqa: BLE001 - a provider bug must not crash the worker
        logger.warning(
            "email delivery failed: provider=%s purpose=%s to=%s error=%s",
            getattr(mailer, "name", "?"),
            message.purpose,
            mask_address(message.to),
            type(exc).__name__,
        )


def _ip_hash(request: Request) -> str:
    ip = client_ip(request.scope, int(get_settings().TRUSTED_PROXY_COUNT))
    return accounts.requester_hash(ip)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.post("/auth/forgot-password", status_code=202, response_model=schemas.AcceptedResponse)
def forgot_password(
    request: Request,
    body: schemas.ForgotPasswordRequest,
    background: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    mailer: Mailer,
) -> schemas.AcceptedResponse:
    """Always 202 with the same body: whether or not the account exists, is active,
    is a protected demo account or has a verified email.  The reset email goes
    only to an existing, active, non-protected account."""
    guard_forgot_password(request, body.email)  # before any database read
    messages = accounts.request_password_reset(
        db, email=body.email, organization=body.organization, ip_hash=_ip_hash(request)
    )
    for message in messages:
        background.add_task(_deliver, mailer, message)
    return schemas.AcceptedResponse(message=accounts.FORGOT_PASSWORD_ACCEPTED)


@router.post("/auth/reset-password", status_code=204, response_class=Response)
def reset_password(
    body: schemas.ResetPasswordRequest,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Redeem a reset token; every existing session of the account is revoked."""
    accounts.reset_password(db, raw_token=body.token, new_password=body.new_password)
    return Response(status_code=204)


@router.post("/auth/change-password", response_model=schemas.TokenResponse)
def change_password(
    body: schemas.ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.TokenResponse:
    """Change the signed-in user's password; other sessions end, this one is re-issued."""
    return accounts.change_password(
        db,
        user=current_user,
        current_password=body.current_password,
        new_password=body.new_password,
    )


@router.post("/auth/send-verification", status_code=202, response_model=schemas.AcceptedResponse)
def send_verification(
    request: Request,
    background: BackgroundTasks,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    mailer: Mailer,
) -> schemas.AcceptedResponse:
    """Email the signed-in user a verification link (no-op when already verified)."""
    message = accounts.send_verification(db, user=current_user, ip_hash=_ip_hash(request))
    if message is None:
        return schemas.AcceptedResponse(message=accounts.ALREADY_VERIFIED)
    background.add_task(_deliver, mailer, message)
    return schemas.AcceptedResponse(message=accounts.VERIFICATION_SENT)


@router.post("/auth/verify-email", status_code=204, response_class=Response)
def verify_email(
    body: schemas.VerifyEmailRequest,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Redeem an email-verification token."""
    accounts.verify_email(db, raw_token=body.token)
    return Response(status_code=204)


@router.get("/auth/invite/{token}", response_model=schemas.InvitePeekResponse)
def peek_invite(
    token: str,
    response: Response,
    db: Annotated[Session, Depends(get_db)],
) -> schemas.InvitePeekResponse:
    """Organisation, masked email and name for the accept page; generic 404 otherwise."""
    _no_store(response)
    peek = accounts.peek_invite(db, raw_token=token)
    return schemas.InvitePeekResponse(
        organization_name=peek.organization_name,
        email_masked=peek.email_masked,
        name=peek.name,
        expires_at=peek.expires_at,
    )


@router.post("/auth/accept-invite", status_code=204, response_class=Response)
def accept_invite(
    body: schemas.AcceptInviteRequest,
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Choose a password and activate the invited account (no session is issued)."""
    accounts.accept_invite(db, raw_token=body.token, password=body.password, name=body.name)
    return Response(status_code=204)


def _invite_response(
    db: Session, issued: accounts.IssuedInvite, mailer: EmailProvider, response: Response
) -> schemas.InviteResponse:
    _no_store(response)
    return schemas.InviteResponse(
        user=admin.to_response(db, issued.user),
        # Only when nothing is delivered: the admin hands the link over by hand.
        invite_url=None if mailer.delivers else issued.link,
        invite_expires_at=issued.expires_at,
        email_provider=mailer.name,
    )


@router.post("/users/invite", response_model=schemas.InviteResponse, status_code=201)
def invite_user(
    body: schemas.UserInviteRequest,
    response: Response,
    background: BackgroundTasks,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
    mailer: Mailer,
) -> schemas.InviteResponse:
    """Invite a user: the account exists (inactive) until they accept."""
    issued = accounts.invite_user(db, admin=current_user, body=body)
    background.add_task(_deliver, mailer, issued.message)
    return _invite_response(db, issued, mailer, response)


@router.post("/users/{user_id}/invite/resend", response_model=schemas.InviteResponse)
def resend_invite(
    user_id: uuid.UUID,
    response: Response,
    background: BackgroundTasks,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
    mailer: Mailer,
) -> schemas.InviteResponse:
    """A fresh invitation link (earlier links stop working)."""
    issued = accounts.resend_invite(db, admin=current_user, user_id=user_id)
    background.add_task(_deliver, mailer, issued.message)
    return _invite_response(db, issued, mailer, response)


@router.post("/users/{user_id}/invite/revoke", response_model=schemas.UserResponse)
def revoke_invite(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(require_user_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> schemas.UserResponse:
    """Revoke the invitation: its links stop working and the account stays inactive."""
    user = accounts.revoke_invite(db, admin=current_user, user_id=user_id)
    return admin.to_response(db, user)
