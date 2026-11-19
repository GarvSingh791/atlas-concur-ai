"""User administration — create / edit / deactivate / reactivate (product gap 3).

Finance-admin only (the router gates every endpoint on ``users.read`` AND the
``finance_admin`` role).  Every function here is org-scoped: a target user
outside the admin's organization is a 404, indistinguishable from a missing id.

Governance safeguards (each one tested in backend/tests/test_pg_users_admin_db.py):

  * the LAST active finance admin of an organization can never be deactivated or
    demoted (checked under a row lock on the org's active finance admins, so two
    admins demoting each other concurrently cannot both succeed);
  * an admin can never deactivate or demote THEMSELVES;
  * a user cannot be their own manager, and a manager change can never create a
    cycle in the reporting chain;
  * a manager must be an ACTIVE manager / finance admin of the same org;
  * a user with active direct reports cannot be deactivated or moved to the
    employee role (their reports' approvals would route to someone who cannot
    approve) — reassign the reports first;
  * email is unique per organization (case-insensitive).  The same address may
    exist in another organization: login takes an optional organization login
    code (migration 0022), so a duplicate elsewhere can neither lock out nor
    shadow that tenant's user.  The pre-check only ever looks at the admin's OWN
    organization, so the 409 EMAIL_TAKEN can never reveal another tenant's
    account; the unique index ``uq_users_org_email_lower`` closes the race
    between the check and the insert;
  * every change writes an audit row; the password never appears in audit,
    logs or responses;
  * the traveller grade (AIR-04) must be on the ladder (config/constants/grades.py)
    and an admin can never change their OWN grade — a grade grants travel
    entitlements, so nobody sets their own (segregation of duties).

Deactivation takes effect immediately for existing sessions: get_current_user
and the refresh endpoint reload the user and reject a non-active status on
every request, so outstanding access / refresh tokens stop working at once.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.core.audit import record_audit
from backend.app.core.demo import guard_protected_account
from backend.app.core.errors import ConflictError, NotFoundError, UnprocessableEntityError
from backend.app.core.security import hash_password
from backend.app.modules.identity.password_policy import (
    has_usable_password,
    password_problem,
    unusable_password_hash,
)
from backend.app.modules.identity.schemas import (
    UserCreateRequest,
    UserInviteRequest,
    UserResponse,
    UserUpdateRequest,
)
from backend.app.modules.identity.service import with_manager_names
from config.constants import grades as grade_ladder
from config.settings import get_settings
from database.models.identity import RoleKey, User, UserStatus

# Machine codes for governance refusals (409, standard envelope).
LAST_FINANCE_ADMIN = "LAST_FINANCE_ADMIN"
SELF_ADMIN_CHANGE = "SELF_ADMIN_CHANGE"
MANAGER_CYCLE = "MANAGER_CYCLE"
HAS_DIRECT_REPORTS = "HAS_DIRECT_REPORTS"
EMAIL_TAKEN = "EMAIL_TAKEN"
USER_STATUS_UNCHANGED = "USER_STATUS_UNCHANGED"
SELF_GRADE_CHANGE = "SELF_GRADE_CHANGE"
INVITE_PENDING = "INVITE_PENDING"

# Roles that may be someone's manager (they hold approval.decide.reports).
_MANAGER_ROLES = frozenset({RoleKey.manager, RoleKey.finance_admin})

# Pragmatic shape check (one @, a dot in the domain, no whitespace); deliverability
# is out of scope.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Upper bound on the manager-chain walk (cycle check); real chains are short.
_MAX_CHAIN_DEPTH = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invalid(message: str) -> UnprocessableEntityError:
    return UnprocessableEntityError(message)


def _parse_role(raw: str) -> RoleKey:
    try:
        return RoleKey(raw.strip())
    except ValueError as exc:
        valid = ", ".join(r.value for r in RoleKey)
        raise _invalid(f"Invalid role '{raw}'. Valid roles: {valid}.") from exc


def _clean_name(raw: str) -> str:
    name = " ".join(raw.split())
    if not name:
        raise _invalid("Full name is required.")
    return name


def _clean_department(raw: str | None) -> str | None:
    if raw is None:
        return None
    dept = " ".join(raw.split())
    return dept or None


def _clean_email(raw: str) -> str:
    email = raw.strip().lower()
    if not _EMAIL_RE.match(email):
        raise _invalid("Enter a valid email address.")
    return email


def _parse_grade(raw: str | None) -> str | None:
    """A ladder code (code or title, any case), or None for an empty / null grade."""
    if raw is None or not raw.strip():
        return None
    try:
        return grade_ladder.normalize_grade(raw)
    except ValueError as exc:
        valid = ", ".join(grade_ladder.GRADE_CODES)
        raise _invalid(f"Invalid grade '{raw.strip()[:64]}'. Valid grades: {valid}.") from exc


def _snapshot(user: User) -> dict[str, Any]:
    """Audit snapshot — identity fields only (never password_hash)."""
    return {
        "name": user.name,
        "email": user.email,
        "role": user.role.value,
        "department": user.department,
        "manager_id": str(user.manager_id) if user.manager_id else None,
        "status": user.status.value,
        "grade": user.grade,
    }


def load_org_user(
    session: Session, *, admin: User, user_id: uuid.UUID, for_update: bool = False
) -> User:
    """Load a user in the admin's organization (404 otherwise — no existence leak)."""
    stmt = sa.select(User).where(User.id == user_id, User.organization_id == admin.organization_id)
    if for_update:
        stmt = stmt.with_for_update()
    user = session.scalars(stmt).first()
    if user is None:
        raise NotFoundError("User not found.")
    return user


def to_response(session: Session, user: User) -> UserResponse:
    return with_manager_names(session, [user])[0]


# Either per-organization email constraint means "taken in this organization":
# the case-insensitive index (0022) and the original case-sensitive constraint.
_EMAIL_UNIQUE_CONSTRAINTS = frozenset(
    {"uq_users_org_email_lower", "uq_users_organization_id_email"}
)


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


_EMAIL_TAKEN_MESSAGE = "A user with this email already exists."
_EMAIL_REFUSED_ACTION = "user.create_refused"
_EMAIL_REFUSED_ENTITY = "user_email"
# Fixed namespace for the audit entity id of a refused email (uuid5 of its keyed
# digest): repeated attempts at the same address correlate, nothing is revealed.
_EMAIL_AUDIT_NAMESPACE = uuid.UUID("6f1d3c2a-9b7e-5d4f-8a21-0c9e7b3f5a10")


def _email_digest(email: str) -> str:
    """Keyed (HMAC-SHA256) digest of a normalised email — correlates repeated
    attempts in the audit log without storing the address, and cannot be
    reversed by a dictionary attack without the server secret."""
    key = get_settings().JWT_SECRET.encode("utf-8")
    return hmac.new(key, email.encode("utf-8"), hashlib.sha256).hexdigest()


def _mask_email(email: str) -> str:
    """``j***@example.test`` — first character of the local part and the domain."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _email_taken(session: Session, *, admin: User, email: str) -> ConflictError:
    """Audit a refused account creation and return the uniform 409.

    Only a clash inside the admin's OWN organization ever reaches here (other
    organizations are never consulted).  The audit row lives in the admin's
    organization and carries the actor, a masked email and a keyed digest —
    never the raw address.  It is committed before the
    error is raised (the request's transaction is otherwise discarded); nothing
    else is pending at either call site (the pre-check runs before any write and
    the racing insert was rolled back to its savepoint).
    """
    digest = _email_digest(email)
    record_audit(
        session,
        organization_id=admin.organization_id,
        action=_EMAIL_REFUSED_ACTION,
        entity_type=_EMAIL_REFUSED_ENTITY,
        entity_id=uuid.uuid5(_EMAIL_AUDIT_NAMESPACE, digest),
        actor_id=admin.id,
        before_state=None,
        after_state={
            "reason": EMAIL_TAKEN,
            "email_masked": _mask_email(email),
            "email_hmac_sha256": digest,
        },
    )
    session.commit()
    return ConflictError(_EMAIL_TAKEN_MESSAGE, code=EMAIL_TAKEN)


def _ensure_email_free(
    session: Session, *, admin: User, email: str, exclude: uuid.UUID | None = None
) -> None:
    # Org-scoped: email is unique per organization (migration 0022), and the
    # check must never probe another tenant.
    stmt = sa.select(User.id).where(
        User.organization_id == admin.organization_id, sa.func.lower(User.email) == email
    )
    if exclude is not None:
        stmt = stmt.where(User.id != exclude)
    if session.scalar(stmt) is not None:
        raise _email_taken(session, admin=admin, email=email)


def _validate_manager(
    session: Session,
    *,
    organization_id: uuid.UUID,
    manager_id: uuid.UUID,
    subject_id: uuid.UUID | None,
) -> None:
    """The manager must be an active manager/finance admin of the same org, not the
    subject itself, and the new edge must not close a cycle."""
    if subject_id is not None and manager_id == subject_id:
        raise _invalid("A user cannot be their own manager.")
    manager = session.scalars(
        sa.select(User).where(User.id == manager_id, User.organization_id == organization_id)
    ).first()
    if manager is None:
        raise _invalid("The selected manager does not exist in your organization.")
    if manager.status != UserStatus.active:
        raise _invalid("The selected manager is inactive.")
    if manager.role not in _MANAGER_ROLES:
        raise _invalid("The selected manager must have the manager or finance admin role.")
    if subject_id is None:
        return  # a brand-new user has no reports, so no cycle is possible
    # Walk up from the proposed manager; reaching the subject means a cycle.
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = manager_id
    for _ in range(_MAX_CHAIN_DEPTH):
        if current is None:
            return
        if current == subject_id:
            raise ConflictError(
                "That manager reports (directly or indirectly) to this user; "
                "the change would create a reporting cycle.",
                code=MANAGER_CYCLE,
            )
        if current in seen:
            return  # a pre-existing cycle elsewhere; it never reaches the subject
        seen.add(current)
        current = session.scalar(
            sa.select(User.manager_id).where(
                User.id == current, User.organization_id == organization_id
            )
        )
    raise ConflictError("The reporting chain is too deep to verify.", code=MANAGER_CYCLE)


def _active_report_count(session: Session, user: User) -> int:
    return (
        session.scalar(
            sa.select(sa.func.count())
            .select_from(User)
            .where(
                User.manager_id == user.id,
                User.organization_id == user.organization_id,
                User.status == UserStatus.active,
            )
        )
        or 0
    )


def _guard_finance_admin_removal(session: Session, *, admin: User, target: User) -> None:
    """Refuse removing the org's last active finance admin, then self-removal.

    Locks every active finance-admin row of the org first so two concurrent
    removals serialise and the second one sees the first one's effect.
    """
    others = session.scalars(
        sa.select(User.id)
        .where(
            User.organization_id == target.organization_id,
            User.role == RoleKey.finance_admin,
            User.status == UserStatus.active,
            User.id != target.id,
        )
        .with_for_update()
    ).all()
    if target.role == RoleKey.finance_admin and target.status == UserStatus.active and not others:
        raise ConflictError(
            "This is the organization's last active finance admin; "
            "promote another user to finance admin first.",
            code=LAST_FINANCE_ADMIN,
        )
    if target.id == admin.id:
        raise ConflictError(
            "You cannot deactivate or demote your own account.", code=SELF_ADMIN_CHANGE
        )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def build_new_user(
    session: Session,
    *,
    admin: User,
    body: UserCreateRequest | UserInviteRequest,
    password: str | None,
) -> User:
    """Validate a new account's fields (shared by POST /users and POST /users/invite)
    and insert it in the admin's organization (flushed, not committed).

    ``password`` given: an ACTIVE account with that (strength-checked) password.
    ``password`` None (an invitation): an INACTIVE account with an unusable
    password hash, activated when the invitee accepts.
    """
    email = _clean_email(body.email)
    name = _clean_name(body.name)
    role = _parse_role(body.role)
    department = _clean_department(body.department)
    grade = _parse_grade(body.grade)
    if password is not None:
        problem = password_problem(password, email=email)
        if problem is not None:
            raise _invalid(problem)
    if body.manager_id is not None:
        _validate_manager(
            session,
            organization_id=admin.organization_id,
            manager_id=body.manager_id,
            subject_id=None,
        )
    _ensure_email_free(session, admin=admin, email=email)

    user = User(
        organization_id=admin.organization_id,
        name=name,
        email=email,
        role=role,
        department=department,
        manager_id=body.manager_id,
        status=UserStatus.active if password is not None else UserStatus.inactive,
        password_hash=hash_password(password) if password is not None else unusable_password_hash(),
        grade=grade,
    )
    try:
        with session.begin_nested():
            session.add(user)
            session.flush()
    except IntegrityError as exc:
        # Lost the race against a concurrent create of the same email in this
        # org: the per-org unique index refused the insert.  Same answer as the
        # pre-check; any other integrity failure propagates unchanged.
        if _violated_constraint(exc) in _EMAIL_UNIQUE_CONSTRAINTS:
            raise _email_taken(session, admin=admin, email=email) from None
        raise
    return user


def snapshot(user: User) -> dict[str, Any]:
    return _snapshot(user)


def create_user(session: Session, *, admin: User, body: UserCreateRequest) -> UserResponse:
    user = build_new_user(session, admin=admin, body=body, password=body.password)
    record_audit(
        session,
        organization_id=admin.organization_id,
        action="user.created",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        before_state=None,
        after_state=_snapshot(user),
    )
    session.commit()
    return to_response(session, user)


def update_user(
    session: Session, *, admin: User, user_id: uuid.UUID, body: UserUpdateRequest
) -> UserResponse:
    user = load_org_user(session, admin=admin, user_id=user_id, for_update=True)
    before = _snapshot(user)
    fields = body.model_fields_set
    if fields:  # phase 11: a shared demo account is never edited (DEMO_MODE)
        guard_protected_account(session, admin=admin, target=user, operation="update")

    if "name" in fields:
        if body.name is None:
            raise _invalid("Full name is required.")
        user.name = _clean_name(body.name)
    if "department" in fields:
        user.department = _clean_department(body.department)
    if "role" in fields:
        if body.role is None:
            raise _invalid("Role is required.")
        new_role = _parse_role(body.role)
        if new_role != user.role:
            if user.role == RoleKey.finance_admin:
                _guard_finance_admin_removal(session, admin=admin, target=user)
            if new_role not in _MANAGER_ROLES and _active_report_count(session, user) > 0:
                raise ConflictError(
                    "This user still manages active direct reports; reassign them "
                    "before changing the role to employee.",
                    code=HAS_DIRECT_REPORTS,
                )
            user.role = new_role
    if "grade" in fields:
        new_grade = _parse_grade(body.grade)
        if new_grade != user.grade:
            if user.id == admin.id:
                raise ConflictError(
                    "You cannot change your own grade; another finance admin must do it.",
                    code=SELF_GRADE_CHANGE,
                )
            user.grade = new_grade
    if "manager_id" in fields and body.manager_id != user.manager_id:
        if body.manager_id is not None:
            _validate_manager(
                session,
                organization_id=user.organization_id,
                manager_id=body.manager_id,
                subject_id=user.id,
            )
        user.manager_id = body.manager_id

    after = _snapshot(user)
    changed = {k for k in after if after[k] != before[k]}
    if changed:
        session.flush()
        record_audit(
            session,
            organization_id=admin.organization_id,
            action="user.updated",
            entity_type="user",
            entity_id=user.id,
            actor_id=admin.id,
            before_state={k: before[k] for k in sorted(changed)},
            after_state={k: after[k] for k in sorted(changed)},
        )
        session.commit()
    return to_response(session, user)


def set_user_status(
    session: Session, *, admin: User, user_id: uuid.UUID, status: UserStatus
) -> UserResponse:
    user = load_org_user(session, admin=admin, user_id=user_id, for_update=True)
    if user.status == status:
        raise ConflictError(f"User is already {status.value}.", code=USER_STATUS_UNCHANGED)
    if status == UserStatus.active and not has_usable_password(user.password_hash):
        # Phase 12 follow-ups: an invited account has no password until the
        # invitee accepts; activating it would create a login nobody can use.
        raise ConflictError(
            "This user hasn't accepted their invitation yet. Resend the invitation instead.",
            code=INVITE_PENDING,
        )
    if status == UserStatus.inactive:
        # Phase 11: a shared demo account is never deactivated (DEMO_MODE).
        guard_protected_account(session, admin=admin, target=user, operation="deactivate")
        _guard_finance_admin_removal(session, admin=admin, target=user)
        if _active_report_count(session, user) > 0:
            raise ConflictError(
                "This user still manages active direct reports; reassign them first.",
                code=HAS_DIRECT_REPORTS,
            )
    before = user.status.value
    user.status = status
    session.flush()
    record_audit(
        session,
        organization_id=admin.organization_id,
        action="user.deactivated" if status == UserStatus.inactive else "user.reactivated",
        entity_type="user",
        entity_id=user.id,
        actor_id=admin.id,
        before_state={"status": before},
        after_state={"status": status.value},
    )
    session.commit()
    return to_response(session, user)
