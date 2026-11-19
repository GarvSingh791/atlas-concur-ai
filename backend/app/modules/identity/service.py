"""Identity service — authentication business logic.

Security invariants enforced here:
  1. Wrong password and unknown email return the SAME generic UnauthorizedError
     (no user enumeration).
  2. A constant-time-ish code path runs verify_password even when the user is
     not found (dummy hash), so timing differences do not reveal account existence.
  3. Inactive users cannot authenticate.
  3a. Without an organization login code, the password is verified against
     every active account with the email, dummy-padded to EXACTLY
     NO_ORG_LOGIN_CANDIDATES verifies; unless exactly one hash verifies, or if
     more accounts than that share the email, the login is refused with the
     same error (the organization code is then required).
  4. password_hash is never returned, logged, or included in after_state.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.app.core.demo import protected_user_ids
from backend.app.core.errors import (
    AmbiguousCredentialsError,
    BadRequestError,
    NotFoundError,
    UnauthorizedError,
)
from backend.app.core.pagination import Page, PaginationParams, parse_filters, parse_sort
from backend.app.core.rbac import scope_query
from backend.app.core.security import (
    DUMMY_HASH,
    create_access_token,
    create_refresh_token,
    decode_token,
    ensure_token_current,
    verify_password,
)
from backend.app.core.text_search import contains_ci
from backend.app.modules.identity.password_policy import has_usable_password
from backend.app.modules.identity.schemas import (
    AccessTokenResponse,
    OrganizationResponse,
    TokenResponse,
    UserResponse,
)
from database.models.auth_token import AuthToken, AuthTokenPurpose
from database.models.identity import (
    Organization,
    RoleKey,
    User,
    UserStatus,
    normalise_org_slug,
)

logger = logging.getLogger(__name__)

# Explicit allow-lists for deny-by-default filter/sort parsing.
# ``q`` (product gap 3): case-insensitive substring over name OR email, with LIKE
# wildcards escaped (backend/app/core/text_search.py).
USER_FILTER_FIELDS: set[str] = {"role", "department", "status", "email", "name", "q"}
USER_SORT_FIELDS: set[str] = {"name", "email", "role", "department", "status", "created_at"}

# Maps filterable field names to the corresponding ORM column attribute.
_FILTER_COLUMNS = {
    "name": User.name,
    "email": User.email,
    "department": User.department,
}


def _apply_filters(stmt: sa.Select, filters: dict[str, str]) -> sa.Select:
    """Translate the allow-listed filter dict into WHERE clauses."""
    for field, value in filters.items():
        if field == "role":
            try:
                stmt = stmt.where(User.role == RoleKey(value))
            except ValueError as exc:
                valid = ", ".join(r.value for r in RoleKey)
                raise BadRequestError(f"Invalid role '{value}'. Valid values: {valid}.") from exc
        elif field == "status":
            try:
                stmt = stmt.where(User.status == UserStatus(value))
            except ValueError as exc:
                valid = ", ".join(s.value for s in UserStatus)
                raise BadRequestError(f"Invalid status '{value}'. Valid values: {valid}.") from exc
        elif field == "q":
            term = value.strip()
            if term:
                stmt = stmt.where(
                    sa.or_(contains_ci(User.name, term), contains_ci(User.email, term))
                )
        else:
            col = _FILTER_COLUMNS[field]
            stmt = stmt.where(col == value)
    return stmt


def with_manager_names(session: Session, users: list[User]) -> list[UserResponse]:
    """UserResponse rows with ``manager_name`` resolved in ONE batched, org-guarded query."""
    manager_ids = {u.manager_id for u in users if u.manager_id is not None}
    names: dict[uuid.UUID, str] = {}
    if manager_ids:
        org_ids = {u.organization_id for u in users}
        names = {
            mid: name
            for mid, name in session.execute(
                sa.select(User.id, User.name).where(
                    User.id.in_(manager_ids), User.organization_id.in_(org_ids)
                )
            ).all()
        }
    protected = protected_user_ids(session, users)
    invites = pending_invites(session, users)
    out: list[UserResponse] = []
    for u in users:
        row = UserResponse.model_validate(u)
        row.manager_name = names.get(u.manager_id) if u.manager_id is not None else None
        row.demo_protected = u.id in protected
        row.invite_pending = u.id in invites
        row.invite_expires_at = invites.get(u.id)
        row.awaiting_activation = _awaiting_activation(u)
        out.append(row)
    return out


def _awaiting_activation(user: User) -> bool:
    """Invited and never activated: inactive with no usable password yet."""
    return user.status == UserStatus.inactive and not has_usable_password(user.password_hash)


def pending_invites(session: Session, users: list[User]) -> dict[uuid.UUID, datetime]:
    """Invited, never-activated accounts among ``users`` -> their open invitation's
    expiry (ONE batched query; only rows without a usable password qualify)."""
    placeholders = {u.id for u in users if _awaiting_activation(u)}
    if not placeholders:
        return {}
    rows = session.execute(
        sa.select(AuthToken.user_id, sa.func.max(AuthToken.expires_at))
        .where(
            AuthToken.user_id.in_(placeholders),
            AuthToken.purpose == AuthTokenPurpose.invite.value,
            AuthToken.used_at.is_(None),
        )
        .group_by(AuthToken.user_id)
    ).all()
    return {user_id: expires for user_id, expires in rows}


def _apply_sort(stmt: sa.Select, sort: list[tuple[str, str]]) -> sa.Select:
    """Translate the allow-listed sort spec into ORDER BY clauses."""
    for field, direction in sort:
        col = getattr(User, field)
        stmt = stmt.order_by(col.desc() if direction == "desc" else col.asc())
    return stmt


# Without an organization code, login verifies the password against the
# active accounts with the email, padded with the dummy hash to EXACTLY this
# many verifies on EVERY attempt; more accounts than this fail closed (the
# organization code is then required).  Phase 11: bounds bcrypt work per
# attempt.  See ``login``.
NO_ORG_LOGIN_CANDIDATES = 5


def login(
    session: Session, *, email: str, password: str, organization: str | None = None
) -> TokenResponse:
    """Verify credentials and issue access + refresh tokens.

    Same error regardless of whether the email exists or the password is wrong.
    The email is matched case-insensitively (trimmed, lower-cased): admin-created
    accounts are stored lower-case while users type any casing, and legacy
    mixed-case rows still resolve.

    Organization-aware (known limits, migration 0022): an email is unique per
    organization only, so the caller may name the organization by its login code
    (``organizations.slug``, case-insensitive).

    * code given  -> the account is looked up inside THAT organization only
      (uq_users_org_email_lower guarantees at most one row); exactly ONE bcrypt
      verify (the dummy hash when there is no row).
    * no code     -> the password is verified against the K
      (``NO_ORG_LOGIN_CANDIDATES``) OLDEST active accounts with that email
      (created_at, id ascending), padded with the dummy hash to exactly K
      verifies.  It succeeds only when exactly one hash verifies; none or
      several -> fail closed.  Phase 11 integration review fix: oldest first,
      so newer duplicates created by admins of other organisations can never
      crowd out an established account (an earlier version tried the K newest
      and pushed victims out; the phase-11 branch then failed every login
      closed once more than K accounts existed, which let 5 attacker tenants
      lock a victim's no-code login out).  An account beyond the K oldest signs
      in with its organization code (the login page always offers it).

    Why constant K (review fix): the earlier "exactly one active account must
    exist" rule was an existence oracle — an admin could create E in their own
    org with a password they chose and log in as E without a code: 401 meant
    "E is active in another org".  With up to K-1 other accounts the probe is
    still a candidate and logs in either way; only when E already has K or more
    active accounts elsewhere does the (newest) probe fall outside the K
    oldest — a residual, documented in the phase-11 integration log.

    Every failure is the SAME generic 401.  The cap / ambiguous failures raise
    :class:`AmbiguousCredentialsError` (identical response) so the login guard
    does not count them against the email's soft signal.  Only a count is
    logged, never the email.
    """
    email_key = email.strip().lower()
    org_key = normalise_org_slug(organization)
    stmt = sa.select(User).where(sa.func.lower(User.email) == email_key)
    if org_key is not None:
        stmt = stmt.join(Organization, Organization.id == User.organization_id).where(
            Organization.slug == org_key
        )
        user = session.scalars(stmt.limit(1)).first()
        if user is None:
            # Constant-time-ish: still run the hash verify against a dummy value
            verify_password(password, DUMMY_HASH)
            raise UnauthorizedError("Invalid credentials.")
        # Phase 12 review fix: an invitation placeholder (``!unusable:…``) is not
        # a bcrypt hash, so verifying it would fail in microseconds and reveal a
        # pending / revoked invitation.  Verify the dummy hash instead (same full
        # bcrypt work) and fail regardless of the result.
        usable = has_usable_password(user.password_hash)
        stored = user.password_hash if usable else DUMMY_HASH
        if not (verify_password(password, stored) and usable):
            raise UnauthorizedError("Invalid credentials.")
    else:
        # Candidates: the K OLDEST active accounts with the email (created_at,
        # then id, ascending).  Integration review fix: newer duplicates created
        # by admins of other organisations can never crowd out an established
        # account; an account beyond the cap signs in with its organization code.
        # Every attempt runs exactly K verifies (real hashes padded with the
        # dummy hash), so the work never depends on what exists.
        rows = session.scalars(
            stmt.where(User.status == UserStatus.active)
            .order_by(User.created_at.asc(), User.id.asc())
            .limit(NO_ORG_LOGIN_CANDIDATES + 1)
        ).all()
        over_cap = len(rows) > NO_ORG_LOGIN_CANDIDATES
        candidates = list(rows[:NO_ORG_LOGIN_CANDIDATES])
        hashes = [c.password_hash for c in candidates]
        hashes += [DUMMY_HASH] * max(0, NO_ORG_LOGIN_CANDIDATES - len(hashes))
        # Verify ALL K (no short-circuit) so the work never depends on the data.
        verified = [verify_password(password, h) for h in hashes]
        hits = [c for c, ok in zip(candidates, verified, strict=False) if ok]
        if len(hits) != 1:
            if len(hits) > 1:
                logger.warning(
                    "login refused: password matches %d accounts and no organization "
                    "was given (ambiguous, failing closed)",
                    len(hits),
                )
                raise AmbiguousCredentialsError("Invalid credentials.")
            if over_cap:
                logger.warning(
                    "login refused: more than %d active accounts share the email, only "
                    "the oldest %d were tried and none matched (organization code needed "
                    "for the others)",
                    NO_ORG_LOGIN_CANDIDATES,
                    NO_ORG_LOGIN_CANDIDATES,
                )
                raise AmbiguousCredentialsError("Invalid credentials.")
            raise UnauthorizedError("Invalid credentials.")
        user = hits[0]

    if user.status != UserStatus.active:
        raise UnauthorizedError("Invalid credentials.")

    return issue_tokens(user)


def issue_tokens(user: User) -> TokenResponse:
    """Access + refresh tokens bound to the user's current password version."""
    changed = user.password_changed_at
    return TokenResponse(
        access_token=create_access_token(
            user.id, user.organization_id, password_changed_at=changed
        ),
        refresh_token=create_refresh_token(
            user.id, user.organization_id, password_changed_at=changed
        ),
    )


def get_organization(session: Session, current_user: User) -> OrganizationResponse:
    """The caller's own organization (id, name, login code) — never another one."""
    org = session.scalars(
        sa.select(Organization).where(Organization.id == current_user.organization_id)
    ).one()
    return OrganizationResponse(id=org.id, name=org.name, login_code=org.slug)


def refresh_access_token(session: Session, *, refresh_token: str) -> AccessTokenResponse:
    """Issue a new access token given a valid refresh token.

    Rejects access tokens, expired tokens, or invalid tokens.
    """
    payload = decode_token(refresh_token, expected_type="refresh")

    try:
        user_id = uuid.UUID(payload["sub"])
        organization_id = uuid.UUID(payload["org"])
    except (ValueError, KeyError) as exc:
        raise UnauthorizedError("Invalid token claims.") from exc

    user = (
        session.query(User)
        .filter(User.id == user_id, User.organization_id == organization_id)
        .first()
    )

    if user is None or user.status != UserStatus.active:
        raise UnauthorizedError("Invalid credentials.")
    # Phase 12 follow-ups: a refresh token minted before a password change is dead.
    ensure_token_current(payload, user)

    return AccessTokenResponse(
        access_token=create_access_token(
            user.id, user.organization_id, password_changed_at=user.password_changed_at
        ),
    )


def list_users(
    session: Session,
    current_user: User,
    pagination: PaginationParams,
    raw_filters: dict[str, str],
    raw_sort: str | None,
) -> Page[UserResponse]:
    """Return a paginated, scoped list of users visible to *current_user*.

    Applies deny-by-default filter/sort parsing, then scope_query for tenant
    isolation + role-based narrowing.  password_hash is never reachable via
    UserResponse — the schema has no such field.
    """
    filters = parse_filters(raw_filters, USER_FILTER_FIELDS)
    sort = parse_sort(raw_sort, USER_SORT_FIELDS)

    base = sa.select(User)
    base = scope_query(
        base,
        user=current_user,
        ownership_column=User.id,
        org_column=User.organization_id,
        session=session,
    )
    base = _apply_filters(base, filters)

    # Count total rows matching the scoped+filtered query before pagination.
    total: int = session.scalar(sa.select(sa.func.count()).select_from(base.subquery())) or 0

    # Apply sort, defaulting to created_at DESC for deterministic ordering.
    if sort:
        base = _apply_sort(base, sort)
    else:
        base = base.order_by(User.created_at.desc())

    base = base.offset(pagination.offset).limit(pagination.limit)
    rows = session.scalars(base).all()

    return Page.create(
        items=with_manager_names(session, list(rows)),
        total=total,
        page=pagination.page,
        size=pagination.size,
    )


def get_user(session: Session, current_user: User, user_id: uuid.UUID) -> UserResponse:
    """Return a single user visible to *current_user*, or 404.

    Out-of-scope ids and nonexistent ids both return NotFoundError (404) so
    existence of records outside the caller's scope is never leaked.
    """
    stmt = sa.select(User).where(User.id == user_id)
    stmt = scope_query(
        stmt,
        user=current_user,
        ownership_column=User.id,
        org_column=User.organization_id,
        session=session,
    )
    user = session.scalars(stmt).first()
    if user is None:
        raise NotFoundError("User not found.")
    return with_manager_names(session, [user])[0]
