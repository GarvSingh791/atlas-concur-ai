"""P7-T1 · item A — RBAC + record-level scoping (direct unit suite).

Targets ``backend.app.core.rbac``: ``scope_query``, ``direct_report_ids``,
``get_user_permissions``, ``require_role``. These are the authorization choke
points; the existing coverage is API-level only (``test_t3_cross_role_scoping``,
``test_rbac``) plus a partial direct suite (``test_record_scoping``). This file
asserts the scope filter directly against a real ``select`` so that breaking the
tenant or role narrowing in ``scope_query`` is caught at the SQL layer.

All tests use the inherited ``db_session`` fixture (live-DB; self-skips if
Postgres is unreachable) — ``scope_query`` issues a sub-query for managers and
``get_user_permissions`` reads ``role_permissions``, so a real session is
required. Fixtures are reused from the parent conftest; none are redefined.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

import backend.tests.factories as f
from backend.app.core.errors import ForbiddenError
from backend.app.core.rbac import (
    direct_report_ids,
    get_user_permissions,
    require_role,
    scope_query,
)
from database.models.expense_report import ExpenseReport
from database.models.identity import Organization, RoleKey, User


def _visible_owner_ids(
    session: Session, *, actor: User, candidates: list[ExpenseReport]
) -> set[uuid.UUID]:
    """Run scope_query for *actor* over the ExpenseReport table and return the
    set of owner_ids it actually yields (intersected with *candidates*)."""
    stmt = sa.select(ExpenseReport)
    stmt = scope_query(
        stmt,
        user=actor,
        ownership_column=ExpenseReport.owner_id,
        org_column=ExpenseReport.organization_id,
        session=session,
    )
    rows = session.scalars(stmt).all()
    candidate_ids = {r.id for r in candidates}
    return {r.owner_id for r in rows if r.id in candidate_ids}


# ---------------------------------------------------------------------------
# scope_query — the role × ownership matrix
# ---------------------------------------------------------------------------


def test_employee_sees_only_own_records(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    """employee role: scope_query narrows to ownership_column == user.id."""
    employee = users_a["employee"]
    manager = users_a["manager"]
    own = f.make_expense_report(db_session, owner=employee)
    managers = f.make_expense_report(db_session, owner=manager)

    visible = _visible_owner_ids(db_session, actor=employee, candidates=[own, managers])

    assert visible == {employee.id}


def test_manager_sees_self_plus_direct_reports_one_level(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    """manager role: scope_query yields {self + direct reports}, ONE level only.

    A report-of-a-report (built via manager_id=employee.id) must NOT be visible —
    direct-report scoping is non-transitive (PRD US-M1).
    """
    manager = users_a["manager"]
    employee = users_a["employee"]  # employee.manager_id == manager.id
    # Second level: a report whose manager is the employee (grand-report).
    grand_report = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=employee.id)

    r_manager = f.make_expense_report(db_session, owner=manager)
    r_employee = f.make_expense_report(db_session, owner=employee)
    r_grand = f.make_expense_report(db_session, owner=grand_report)

    visible = _visible_owner_ids(
        db_session, actor=manager, candidates=[r_manager, r_employee, r_grand]
    )

    assert visible == {manager.id, employee.id}
    assert grand_report.id not in visible  # one level only — not transitive


def test_finance_admin_sees_whole_org(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    """finance_admin role: org-wide — every owner in the tenant is visible."""
    finance = users_a["finance"]
    manager = users_a["manager"]
    employee = users_a["employee"]
    r_finance = f.make_expense_report(db_session, owner=finance)
    r_manager = f.make_expense_report(db_session, owner=manager)
    r_employee = f.make_expense_report(db_session, owner=employee)

    visible = _visible_owner_ids(
        db_session, actor=finance, candidates=[r_finance, r_manager, r_employee]
    )

    assert visible == {finance.id, manager.id, employee.id}


@pytest.mark.parametrize("actor_role", ["employee", "manager", "finance"])
def test_cross_tenant_row_never_returned(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
    actor_role: str,
) -> None:
    """Tenant isolation: NO role (not even finance_admin) ever sees another
    org's row. 'Org-wide' means this org, not all orgs.

    Mutation target: removing the ``org_column == user.organization_id`` filter
    in scope_query makes this matrix fail.
    """
    actor = users_a[actor_role]
    # A report owned by an org-B user — must be invisible to every org-A actor.
    foreign = f.make_expense_report(db_session, owner=users_b["employee"])
    own = f.make_expense_report(db_session, owner=actor)

    visible = _visible_owner_ids(db_session, actor=actor, candidates=[own, foreign])

    assert foreign.owner_id not in visible
    assert users_b["employee"].id not in visible


def test_finance_admin_cross_tenant_is_blocked_even_for_org_wide(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
    users_a: dict[str, User],
    users_b: dict[str, User],
) -> None:
    """Explicit: finance_admin org-wide visibility stops at the tenant boundary.

    A finance_admin in org B must not see an org-A report even though their role
    is unrestricted within their own org.
    """
    finance_b = users_b["finance"]
    org_a_report = f.make_expense_report(db_session, owner=users_a["employee"])

    visible = _visible_owner_ids(db_session, actor=finance_b, candidates=[org_a_report])

    assert visible == set()


def test_unknown_role_is_fail_safe_forbidden(db_session: Session) -> None:
    """An unrecognized role raises ForbiddenError (fail-safe, Dev Rule 9) —
    it must NOT silently fall through to an unfiltered query."""

    class _ForgedRole:
        # Not a RoleKey member — simulates a forged/unknown role value.
        value = "superadmin"

    forged = f.make_user(db_session)
    # Bypass the enum to simulate a corrupt/forged role on the loaded user.
    forged.role = _ForgedRole()  # type: ignore[assignment]

    stmt = sa.select(ExpenseReport)
    with pytest.raises(ForbiddenError):
        scope_query(
            stmt,
            user=forged,
            ownership_column=ExpenseReport.owner_id,
            org_column=ExpenseReport.organization_id,
            session=db_session,
        )


# ---------------------------------------------------------------------------
# direct_report_ids — one-level, org-guarded
# ---------------------------------------------------------------------------


def test_direct_report_ids_returns_exactly_one_level(
    db_session: Session, org_a: Organization, users_a: dict[str, User]
) -> None:
    manager = users_a["manager"]
    employee = users_a["employee"]
    grand = f.make_user(db_session, org=org_a, role=RoleKey.employee, manager_id=employee.id)

    reports = direct_report_ids(manager, db_session)

    assert reports == {employee.id}
    assert grand.id not in reports  # grandchild excluded


def test_direct_report_ids_empty_when_no_reports(db_session: Session, org_a: Organization) -> None:
    lonely = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    assert direct_report_ids(lonely, db_session) == set()


def test_direct_report_ids_is_org_guarded(
    db_session: Session,
    org_a: Organization,
    org_b: Organization,
) -> None:
    """The org guard rejects a cross-org manager_id anomaly: a user in org B
    whose manager_id points at an org-A manager is NOT counted as a report."""
    manager_a = f.make_user(db_session, org=org_a, role=RoleKey.manager)
    # Data anomaly: org-B user with manager_id = an org-A manager.
    rogue_b = f.make_user(db_session, org=org_b, role=RoleKey.employee, manager_id=manager_a.id)

    reports = direct_report_ids(manager_a, db_session)

    assert rogue_b.id not in reports
    assert reports == set()


# ---------------------------------------------------------------------------
# get_user_permissions — resolved from role_permissions, never from a claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role_key", ["employee", "manager", "finance"])
def test_get_user_permissions_is_nonempty_per_seeded_role(
    db_session: Session,
    rbac_catalog: None,
    users_a: dict[str, User],
    role_key: str,
) -> None:
    """Each seeded role resolves a non-empty permission set from the DB."""
    perms = get_user_permissions(users_a[role_key], db_session)
    assert isinstance(perms, set)
    assert perms, f"expected a non-empty permission set for role {role_key}"


def test_permission_sets_differ_by_role(
    db_session: Session,
    rbac_catalog: None,
    users_a: dict[str, User],
) -> None:
    """get_user_permissions must differentiate by role (not return a constant).

    employee and finance_admin hold genuinely different permission sets: finance
    holds org-wide read + approval-decide perms an employee lacks, while an
    employee holds the per-own create/update perms finance lacks. (They are NOT
    in a superset relationship — each owns perms the other does not.)
    """
    employee_perms = get_user_permissions(users_a["employee"], db_session)
    finance_perms = get_user_permissions(users_a["finance"], db_session)

    assert employee_perms, "employee should have at least one permission"
    assert finance_perms, "finance should have at least one permission"
    assert employee_perms != finance_perms  # role actually changes the result
    # finance holds approval-decide / org-read perms the employee never has
    assert finance_perms - employee_perms, "finance must hold perms employee lacks"
    # employee holds per-own create perms finance never has → not a superset
    assert employee_perms - finance_perms, "employee must hold perms finance lacks"


# NOTE: a test asserting "get_user_permissions reads the table, not the user's
# role attribute" via an *unseeded* catalog is intentionally NOT included: the
# Enum-typed roles.key column rejects a forged key, and an "empty when unseeded"
# assertion is not pollution-proof (other suite tests leak a committed RBAC
# catalog). The table-driven property is instead proven deterministically by
# test_permission_sets_differ_by_role (two seeded roles resolve to different,
# role-specific sets), which controls its own seed precondition via rbac_catalog.


# ---------------------------------------------------------------------------
# require_role — coarse gate, fail-safe
# ---------------------------------------------------------------------------


def test_require_role_allows_member_and_blocks_non_member(db_session: Session) -> None:
    """require_role's inner dep returns the user when the role matches and
    raises ForbiddenError otherwise."""
    manager = f.make_user(db_session, role=RoleKey.manager)
    employee = f.make_user(db_session, role=RoleKey.employee)

    dep = require_role(RoleKey.manager)

    assert dep(current_user=manager) is manager
    with pytest.raises(ForbiddenError):
        dep(current_user=employee)
