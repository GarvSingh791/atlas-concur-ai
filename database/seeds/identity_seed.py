"""Minimal identity seed — organizations, roles, permissions, role_permissions,
and three demo users (employee, manager, finance_admin).

Run against a freshly migrated database:
  python -m database.seeds.identity_seed

Idempotent at two granularities:
  - RBAC catalog (roles / permissions / role_permissions): row-level idempotent.
    seed_rbac_catalog() can be called on an already-seeded DB and will converge
    to the authoritative catalog (adds new rows; never deletes).
  - Org + users block: guarded by org name "Atlas Demo Corp" (skip if present).

This design means running the seed again after P2-T4 added new permission keys
(policy.read, users.read) correctly inserts those new rows without touching
the existing six rows.

Role → permission mapping (authoritative — defined here per design):
  employee      → expense.read.own, expense.create.own, expense.update.own,
                  policy.read,
                  trip.read.own, trip.create.own, trip.update.own
  manager       → (employee perms) + expense.read.reports, approval.decide.reports,
                  approval.read
  finance_admin → expense.read.own, expense.read.org, approval.read,
                  approval.decide.reports (org-wide override), users.read,
                  audit.read, policy.read, policy.manage, trip.read.own
                  (org-wide; scope_query narrows per role)

P5-T8 additions (add-only, idempotent):
  - approval.read: manager + finance_admin (the approval queue/detail read gate;
    scope_query narrows manager → direct reports, finance_admin → org-wide)
  - approval.decide.reports: also granted to finance_admin (US-F2 AC3 override;
    scope_query widens finance to org-wide on the decision path)

P2-T7 additions (add-only, idempotent):
  - expense.update.own: added to employee + manager (submit + entry edits)
  - expense.read.own: added to finance_admin (mirrors trip.read.own on finance;
    scope_query widens finance to org-wide; existing read.org is a semantic marker)

Product gap 1 additions (add-only, idempotent; also inserted by migration 0016):
  - expense.reimburse.org: finance_admin only — mark an approved report
    reimbursed (simulated payment; never on the caller's own report)

#3b additions (add-only, idempotent):
  - audit.read: finance_admin only — org-wide compliance read of the audit_log
    (no ownership column; the endpoint scopes by organization_id alone)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

# Ensure repo root is on sys.path when run as __main__
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from backend.app.core.database import get_sessionmaker  # noqa: E402
from backend.app.core.security import hash_password  # noqa: E402
from config.constants.permissions import (  # noqa: E402
    APPROVAL_DECIDE_REPORTS,
    APPROVAL_READ,
    AUDIT_READ,
    EXPENSE_CREATE_OWN,
    EXPENSE_READ_ORG,
    EXPENSE_READ_OWN,
    EXPENSE_READ_REPORTS,
    EXPENSE_REIMBURSE_ORG,
    EXPENSE_UPDATE_OWN,
    PERMISSION_CATALOG,  # noqa: E402
    POLICY_MANAGE,
    POLICY_READ,
    TRIP_CREATE_OWN,
    TRIP_READ_OWN,
    TRIP_UPDATE_OWN,
    USERS_READ,
)
from config.settings import get_settings  # noqa: E402
from database.models.identity import (  # noqa: E402
    Organization,
    Permission,
    Role,
    RoleKey,
    RolePermission,
    User,
    UserStatus,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Role → permission mapping (authoritative catalog, references constants)
# ---------------------------------------------------------------------------

_ROLE_PERMISSIONS: dict[RoleKey, list[str]] = {
    RoleKey.employee: [
        EXPENSE_READ_OWN,
        EXPENSE_CREATE_OWN,
        EXPENSE_UPDATE_OWN,  # P2-T7: submit + entry edits
        POLICY_READ,
        TRIP_READ_OWN,
        TRIP_CREATE_OWN,
        TRIP_UPDATE_OWN,
    ],
    RoleKey.manager: [
        EXPENSE_READ_OWN,
        EXPENSE_CREATE_OWN,
        EXPENSE_UPDATE_OWN,  # P2-T7: submit + entry edits on own reports
        EXPENSE_READ_REPORTS,
        APPROVAL_DECIDE_REPORTS,
        APPROVAL_READ,  # P5-T8: read the approval queue/detail (scope → reports)
        POLICY_READ,
        TRIP_READ_OWN,
        TRIP_CREATE_OWN,
        TRIP_UPDATE_OWN,
    ],
    RoleKey.finance_admin: [
        EXPENSE_READ_OWN,  # P2-T7: read gate (scope_query widens to org-wide)
        EXPENSE_READ_ORG,  # semantic marker for org-wide scope
        APPROVAL_READ,  # P5-T8: read the approval queue/detail (scope → org-wide)
        APPROVAL_DECIDE_REPORTS,  # P5-T8 / US-F2 AC3: finance override (scope → org)
        USERS_READ,
        AUDIT_READ,  # #3b: org-wide compliance read of the audit_log
        EXPENSE_REIMBURSE_ORG,  # product gap 1: simulated reimbursement (finance only)
        POLICY_READ,
        POLICY_MANAGE,
        TRIP_READ_OWN,  # org-wide read; scope_query narrows per role
    ],
}


# ---------------------------------------------------------------------------
# RBAC catalog helpers (row-level idempotent — add-only, never delete)
# ---------------------------------------------------------------------------


def upsert_roles(session: Session) -> dict[RoleKey, Role]:
    """Get-or-create one Role row per RoleKey value.  Returns role_key → Role.

    Uses an atomic ``INSERT ... ON CONFLICT DO NOTHING`` on the ``key`` unique
    index so concurrent committed seed sessions cannot race on uq_roles_key.
    """
    rows = [{"key": role_key, "description": f"{role_key.value} role"} for role_key in RoleKey]
    if rows:
        session.execute(pg_insert(Role).values(rows).on_conflict_do_nothing(index_elements=["key"]))
    session.flush()
    # Re-query after flush so all objects (new and pre-existing) are in the map.
    role_objs: dict[RoleKey, Role] = {}
    for role_key in RoleKey:
        role = session.query(Role).filter(Role.key == role_key).first()
        assert role is not None  # insert guarantees existence
        role_objs[role_key] = role
    return role_objs


def upsert_permissions(session: Session) -> dict[str, Permission]:
    """Get-or-create one Permission row per entry in PERMISSION_CATALOG.

    Uses an atomic ``INSERT ... ON CONFLICT DO NOTHING`` on the ``key`` unique
    index so concurrent committed seed sessions cannot race on uq_permissions_key.
    """
    rows = [
        {"key": perm_key, "description": description}
        for perm_key, description in PERMISSION_CATALOG
    ]
    if rows:
        session.execute(
            pg_insert(Permission).values(rows).on_conflict_do_nothing(index_elements=["key"])
        )
    session.flush()
    perm_objs: dict[str, Permission] = {}
    for perm_key, _ in PERMISSION_CATALOG:
        perm = session.query(Permission).filter(Permission.key == perm_key).first()
        assert perm is not None
        perm_objs[perm_key] = perm
    return perm_objs


def sync_role_permissions(
    session: Session,
    role_objs: dict[RoleKey, Role],
    perm_objs: dict[str, Permission],
) -> None:
    """Add any missing (role_id, permission_id) rows.  Never deletes rows.

    Uses an atomic ``INSERT ... ON CONFLICT DO NOTHING`` on the composite PK
    (role_id, permission_id) so concurrent committed seed sessions cannot race.
    """
    rows = [
        {"role_id": role_objs[role_key].id, "permission_id": perm_objs[perm_key].id}
        for role_key, perm_keys in _ROLE_PERMISSIONS.items()
        for perm_key in perm_keys
    ]
    if rows:
        session.execute(
            pg_insert(RolePermission)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["role_id", "permission_id"])
        )
    session.flush()


def seed_rbac_catalog(session: Session) -> None:
    """Idempotently converge roles, permissions, and role_permissions to the
    authoritative catalog.  Commits once.  Safe to call standalone (used by tests).
    """
    role_objs = upsert_roles(session)
    perm_objs = upsert_permissions(session)
    sync_role_permissions(session, role_objs, perm_objs)
    session.commit()


# ---------------------------------------------------------------------------
# Full seed: catalog + demo org + demo users
# ---------------------------------------------------------------------------


def seed(session_factory=None) -> None:  # type: ignore[assignment]
    """Run the identity seed.  Pass a session_factory to override (useful in tests)."""
    if session_factory is None:
        session_factory = get_sessionmaker()

    settings = get_settings()

    with session_factory() as session:
        # ---- RBAC catalog (row-level idempotent) ---------------------------
        seed_rbac_catalog(session)

        # ---- Guard: skip org + users if already seeded ---------------------
        existing_org = (
            session.query(Organization).filter(Organization.name == "Atlas Demo Corp").first()
        )
        if existing_org is not None:
            print("Identity seed already applied — skipping org/users block.")
            return

        # ---- Organization --------------------------------------------------
        # Login code = what migration 0022 derives from the name on an existing DB.
        org = Organization(name="Atlas Demo Corp", base_currency="USD", slug="atlas-demo-corp")
        session.add(org)
        session.flush()  # populate org.id

        # ---- Users ---------------------------------------------------------
        hashed_pw = hash_password(settings.SEED_DEFAULT_PASSWORD)

        manager = User(
            organization_id=org.id,
            name="Morgan Manager",
            email="manager@atlas-demo.internal",
            role=RoleKey.manager,
            department="Finance",
            manager_id=None,  # top-level
            status=UserStatus.active,
            password_hash=hashed_pw,
        )
        session.add(manager)
        session.flush()  # populate manager.id for the employee FK

        employee = User(
            organization_id=org.id,
            name="Alex Employee",
            email="employee@atlas-demo.internal",
            role=RoleKey.employee,
            department="Engineering",
            manager_id=manager.id,
            status=UserStatus.active,
            password_hash=hashed_pw,
        )
        session.add(employee)

        finance_admin = User(
            organization_id=org.id,
            name="Finley Admin",
            email="finance@atlas-demo.internal",
            role=RoleKey.finance_admin,
            department="Finance",
            manager_id=manager.id,
            status=UserStatus.active,
            password_hash=hashed_pw,
        )
        session.add(finance_admin)

        session.commit()

    print("Identity seed complete.")
    print("  Organization : Atlas Demo Corp")
    print("  Users        : employee@, manager@, finance@atlas-demo.internal")
    # Never echo the password itself: seed output lands in CI / Render logs.
    print("  Password     : (the value of SEED_DEFAULT_PASSWORD; not printed)")


if __name__ == "__main__":
    seed()
