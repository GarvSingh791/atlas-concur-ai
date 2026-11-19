"""Permission-key catalog — the single source of truth for all permission strings.

Both the seed (database/seeds/identity_seed.py) and the enforcement layer
(backend/app/core/rbac.py) import from here.  No permission key string literal
should appear anywhere else.

Key format: resource.action.scope  (e.g. expense.read.own)

Catalog is intentionally small (Dev Rule 10): only the keys needed to cover
the three roles' PRD user stories, constrained to models that exist at this
phase.  receipt.* keys are deferred to future phases.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Expense permissions
# ---------------------------------------------------------------------------
EXPENSE_READ_OWN = "expense.read.own"
EXPENSE_CREATE_OWN = "expense.create.own"
EXPENSE_UPDATE_OWN = "expense.update.own"
EXPENSE_READ_REPORTS = "expense.read.reports"
EXPENSE_READ_ORG = "expense.read.org"
# Product gap 1: mark an APPROVED report reimbursed (simulated payment).  Granted
# to finance_admin only; the service additionally refuses the caller's own report
# (segregation of duties).  Added to live databases by migration 0016.
EXPENSE_REIMBURSE_ORG = "expense.reimburse.org"

# ---------------------------------------------------------------------------
# Approval permissions
# ---------------------------------------------------------------------------
APPROVAL_DECIDE_REPORTS = "approval.decide.reports"
# Read gate for the approval queue + detail (P5-T8). Granted to manager AND
# finance_admin; scope_query narrows visibility per role (manager → direct
# reports, finance_admin → org-wide). Mirrors the TRIP_READ_OWN multi-role gate.
APPROVAL_READ = "approval.read"

# ---------------------------------------------------------------------------
# Policy permissions
# ---------------------------------------------------------------------------
POLICY_READ = "policy.read"
POLICY_MANAGE = "policy.manage"

# ---------------------------------------------------------------------------
# User-management permissions
# ---------------------------------------------------------------------------
USERS_READ = "users.read"

# ---------------------------------------------------------------------------
# Audit permissions (#3b)
# ---------------------------------------------------------------------------
# Org-wide compliance read of the append-only audit_log. Granted to
# finance_admin only (the org-wide compliance reader); audit_log has no
# ownership column, so the endpoint scopes by organization_id alone (no
# scope_query). Mirrors EXPENSE_READ_ORG / USERS_READ finance_admin scoping.
AUDIT_READ = "audit.read"

# ---------------------------------------------------------------------------
# Trip permissions (P2-T6)
# ---------------------------------------------------------------------------
TRIP_READ_OWN = "trip.read.own"
TRIP_CREATE_OWN = "trip.create.own"
TRIP_UPDATE_OWN = "trip.update.own"

# ---------------------------------------------------------------------------
# Catalog: ordered (key, description) pairs.
# Used by the seed to populate the permissions table and by tests for fixtures.
# ---------------------------------------------------------------------------
PERMISSION_CATALOG: tuple[tuple[str, str], ...] = (
    (EXPENSE_READ_OWN, "Read own expense reports"),
    (EXPENSE_CREATE_OWN, "Create own expense reports"),
    (EXPENSE_UPDATE_OWN, "Update own expense reports (submit/state) and entries"),
    (EXPENSE_READ_REPORTS, "Read direct reports' expense reports"),
    (EXPENSE_READ_ORG, "Read all expense reports in the organization"),
    (
        EXPENSE_REIMBURSE_ORG,
        "Mark approved expense reports reimbursed (simulated payment, finance only)",
    ),
    (APPROVAL_DECIDE_REPORTS, "Approve or reject direct reports' expense reports"),
    (APPROVAL_READ, "Read approvals in scope (manager → reports, finance → org)"),
    (POLICY_READ, "Read expense policies"),
    (POLICY_MANAGE, "Manage expense policies"),
    (USERS_READ, "Read users in the organization"),
    (AUDIT_READ, "Read the organization's audit log (compliance)"),
    (TRIP_READ_OWN, "Read trips (scoped by role: own / reports / org)"),
    (TRIP_CREATE_OWN, "Create trips"),
    (TRIP_UPDATE_OWN, "Transition trip status"),
)

ALL_PERMISSION_KEYS: frozenset[str] = frozenset(k for k, _ in PERMISSION_CATALOG)

# Added in P2-T7: EXPENSE_UPDATE_OWN (read/create/read.reports/read.org pre-existed from P2-T3)
