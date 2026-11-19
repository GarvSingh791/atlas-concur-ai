"""Server-side RBAC and record-level scope filter for Atlas.

This module is the authorization layer that sits on top of the authentication
seam (get_current_user, P2-T3).  It provides:

  - get_user_permissions: resolves the set of permission keys for a DB-loaded
    user's role from the role_permissions table (never trusts a token claim).
  - require_permission: FastAPI dependency factory — gates an endpoint on a
    specific permission key; raises ForbiddenError (403) on failure.
  - require_role: thin coarse convenience for cases that are genuinely
    role-level; permission keys remain the primary mechanism.
  - direct_report_ids: returns the UUIDs of a manager's direct reports
    (ONE level only — per PRD US-M1).
  - scope_query: the mandatory choke point every domain query MUST route
    through.  Applies tenant isolation (organization_id) first, then narrows
    by role.  Generic: parameterized by ORM column attributes so it works for
    any ownership column (Trip.traveler_id, ExpenseReport.owner_id, …).

  - require_owner / owner_scope_query: the WRITE scope.  Every mutation of a
    user-owned record (edit, submit, cancel, entry edits, …) is owner-only for
    every role — read scope (scope_query) must never be reused to authorize a
    write (C7-01).

IMPORTANT: scope_query is NOT bypassable — every future domain query that
returns user-owned records MUST pass through this function before executing.
The organization_id filter is always applied, even for finance_admin (org-wide
≠ all-orgs).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import Depends
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql import Select

from backend.app.core.database import get_db
from backend.app.core.errors import ForbiddenError
from backend.app.core.security import get_current_user
from database.models.identity import (
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
)

# ---------------------------------------------------------------------------
# Permission resolution
# ---------------------------------------------------------------------------


def get_user_permissions(user: User, session: Session) -> set[str]:
    """Return the set of permission keys for *user*'s role.

    Queries role_permissions via the roles table; does NOT trust any
    role/permission claim that might be in the request token.
    """
    stmt = (
        sa.select(Permission.key)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(Role.key == user.role)
    )
    return set(session.scalars(stmt))


# ---------------------------------------------------------------------------
# Dependency factories
# ---------------------------------------------------------------------------


def require_permission(permission_key: str) -> Callable[..., User]:
    """FastAPI dependency factory — returns a dep that passes if the
    DB-loaded user has *permission_key* in their role's permission set;
    raises ForbiddenError (403, standard envelope) otherwise.

    Usage:
        @router.get("/something")
        def endpoint(user: Annotated[User, Depends(require_permission(EXPENSE_READ_OWN))]):
            ...
    """

    def dep(
        current_user: Annotated[Any, Depends(get_current_user)],
        session: Annotated[Session, Depends(get_db)],
    ) -> User:
        user: User = current_user  # get_current_user returns Any; real type is User
        if permission_key not in get_user_permissions(user, session):
            raise ForbiddenError("You do not have permission to perform this action.")
        return user

    # Give the inner dependency a unique name so FastAPI's dependency cache
    # doesn't collapse two require_permission() calls with different keys.
    dep.__name__ = f"require_permission_{permission_key.replace('.', '_')}"
    return dep


def require_role(*allowed: RoleKey) -> Callable[..., User]:
    """Thin coarse convenience — gates on role membership.

    Prefer require_permission() for fine-grained checks.  Use require_role
    only for cases that are genuinely role-level (e.g. an admin-only
    maintenance endpoint).
    """

    def dep(
        current_user: Annotated[Any, Depends(get_current_user)],
    ) -> User:
        user: User = current_user
        if user.role not in allowed:
            raise ForbiddenError("You do not have permission to perform this action.")
        return user

    dep.__name__ = "require_role_" + "_or_".join(r.value for r in allowed)
    return dep


# ---------------------------------------------------------------------------
# Record-level scope filter (data-access choke point)
# ---------------------------------------------------------------------------


def direct_report_ids(manager: User, session: Session) -> set[uuid.UUID]:
    """Return the set of UUIDs of *manager*'s DIRECT reports (one level only).

    The organization_id guard is belt-and-suspenders against data anomalies
    (e.g. cross-org manager_id set via a migration bug).
    """
    stmt = sa.select(User.id).where(
        User.manager_id == manager.id,
        User.organization_id == manager.organization_id,
    )
    return set(session.scalars(stmt))


def scope_query(
    stmt: Select[Any],
    *,
    user: User,
    ownership_column: InstrumentedAttribute[uuid.UUID],
    org_column: InstrumentedAttribute[uuid.UUID],
    session: Session,
) -> Select[Any]:
    """Apply tenant isolation + role-based record scoping to *stmt*.

    Parameters
    ----------
    stmt:
        The base SELECT statement to narrow.
    user:
        The authenticated, DB-loaded user (from get_current_user).
    ownership_column:
        The ORM column attribute that identifies the record owner, e.g.
        ``Trip.traveler_id`` or ``ExpenseReport.owner_id``.
    org_column:
        The ORM column attribute carrying the tenant key, e.g.
        ``Trip.organization_id``.  MUST be present on every scoped model.
    session:
        Active SQLAlchemy session (for the direct-reports sub-query).

    Returns
    -------
    Select
        The statement with tenant + role filters applied.  The caller is
        responsible for executing it.

    Notes
    -----
    - organization_id is ALWAYS applied first, even for finance_admin.
      "Org-wide" means "all rows in THIS org", not "all rows globally".
    - Direct reports are ONE level (manager_id == manager.id) — not transitive.
    - An unrecognized role raises ForbiddenError (fail-safe per Dev Rule 9).
    """
    # Tenant isolation — unconditional and first.
    stmt = stmt.where(org_column == user.organization_id)

    if user.role == RoleKey.employee:
        stmt = stmt.where(ownership_column == user.id)

    elif user.role == RoleKey.manager:
        report_ids = direct_report_ids(user, session)
        visible_ids: list[uuid.UUID] = [user.id, *report_ids]
        stmt = stmt.where(ownership_column.in_(visible_ids))

    elif user.role == RoleKey.finance_admin:
        # Org-wide: tenant filter already applied; no further narrowing.
        pass

    else:
        # Fail safe (Dev Rule 9): an unknown role sees nothing.
        raise ForbiddenError("Unrecognized role — access denied.")

    return stmt


def narrow_to_caller(
    stmt: Select[Any],
    *,
    user: User,
    ownership_column: InstrumentedAttribute[uuid.UUID],
    mine: bool,
) -> Select[Any]:
    """Optionally narrow an already-scoped list query to rows *user* owns (UAT2).

    The personal pages ("My expenses", "My trips") pass ``mine=true`` so a
    manager or finance_admin sees only their OWN items there — the role's wider
    read scope (team / org-wide) stays the default for every other consumer.

    Apply it AFTER :func:`scope_query`: it only ever ADDS a predicate (AND), so
    it can narrow but never widen — tenant isolation and the unknown-role
    fail-safe have already run.  ``mine=False`` returns *stmt* unchanged.
    """
    if not mine:
        return stmt
    return stmt.where(ownership_column == user.id)


# ---------------------------------------------------------------------------
# Write (mutation) scope — strictly owner-only (C7-01)
# ---------------------------------------------------------------------------
#
# READ scope (scope_query) is deliberately wider than WRITE scope: a manager
# may READ a direct report's trips/reports and finance_admin may READ the whole
# org, but nobody may EDIT, SUBMIT, CANCEL or otherwise mutate a record they do
# not own.  Approve/reject decisions never go through owner mutation endpoints —
# they go through the approval workflow, which has its own authorization.
#
# Mutation paths load the record with scope_query (so an unreadable record is a
# 404 with no existence leak) and then call require_owner (so a readable but
# not-owned record is a 403).


def require_owner(owner_id: uuid.UUID | None, *, user: User) -> None:
    """Raise ForbiddenError (403) unless *user* owns the record.

    Applies to EVERY role, finance_admin included.  ``owner_id`` is the value of
    the record's ownership column (Trip.traveler_id, ExpenseReport.owner_id, the
    parent report's owner for an entry, …).  ``None`` never matches (fail safe).
    """
    if owner_id is None or owner_id != user.id:
        raise ForbiddenError("Only the owner of this record can modify it.")


def owner_scope_query(
    stmt: Select[Any],
    *,
    user: User,
    ownership_column: InstrumentedAttribute[uuid.UUID],
    org_column: InstrumentedAttribute[uuid.UUID],
) -> Select[Any]:
    """Apply tenant isolation + STRICT ownership to *stmt* (write scope).

    Unlike scope_query this is identical for every role: only rows owned by
    *user* inside *user*'s organization survive.  Use it where a not-owned
    record must be indistinguishable from a missing one (e.g. validating a
    reference supplied in a create payload).
    """
    return stmt.where(org_column == user.organization_id, ownership_column == user.id)
