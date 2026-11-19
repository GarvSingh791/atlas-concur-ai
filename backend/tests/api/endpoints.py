"""Live-endpoint registry for the P7-T2 API contract matrix.

This module is the SINGLE source of truth the contract matrix iterates over.
Every spec below is reconciled against the *introspected* live route table (see
``test_contract_matrix.test_registry_covers_every_live_route``) — the gate fails
if a real route is missing from this list or a listed route no longer exists, so
no endpoint can be silently skipped.

Reconcile notes (live surface vs. the P7-T2 brief / TECHNICAL_DESIGN map)
------------------------------------------------------------------------
* Error envelope is NESTED: ``{"error": {code, message, correlation_id}}`` +
  ``X-Correlation-ID`` header (authoritative per docs/phase_logs/P3-T4). The
  brief's flat ``{code, message, correlation_id}`` is shorthand for the inner
  object.
* ``LLM_PROVIDER`` accepts only ``local``/``anthropic`` — the brief's ``mock`` is
  invalid; tests run deterministically under ``local`` (conftest default).
* Idempotency is REPLAY keyed on ``(organization_id, scope, key)`` with NO body
  comparison — see test_idempotency for the flagged divergence from the brief's
  "(e) different body -> 409". A replay is served only to the caller who wrote
  the original: a same-org, DIFFERENT-user reuse of the key returns 409 CONFLICT
  on ``POST /expense-reports`` and ``POST /receipts`` (C7 cross-user replay fix).
* Paginated (``Page`` envelope, deny-by-default filter/sort) collections are the
  five ``/users``, ``/trips``, ``/expense-reports``, ``/approvals`` and ``/audit``.
  ``/policies`` and ``/policy-rules`` are intentionally non-paginated read
  surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

API = "/api/v1"

# Role identifiers — match the keys produced by the ``role_clients`` fixture.
EMPLOYEE = "employee"
MANAGER = "manager"
FINANCE = "finance_admin"
ALL_ROLES = frozenset({EMPLOYEE, MANAGER, FINANCE})

# Auth classes
PUBLIC = "public"  # no token required (login/refresh)
AUTH = "auth"  # any authenticated user (no role gate)
PERM = "perm"  # permission-gated


@dataclass(frozen=True)
class EndpointSpec:
    """One live route + the contract metadata the matrix needs.

    ``path`` is the FastAPI path template (with ``{param}`` placeholders) exactly
    as registered, so the coverage gate can compare against ``app.routes``.
    """

    method: str
    path: str
    label: str
    auth: str = AUTH
    permission: str | None = None
    allowed_roles: frozenset[str] = ALL_ROLES
    forbidden_roles: frozenset[str] = frozenset()
    collection: bool = False  # paginated Page-envelope collection
    idem_scope: str | None = None  # Idempotency-Key scope, if any
    streaming: bool = False  # SSE / StreamingResponse
    multipart: bool = False  # multipart/form-data body (file upload)
    has_path_id: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "has_path_id", "{" in self.path)

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)

    def concrete_path(self, **ids: object) -> str:
        """Fill ``{param}`` placeholders; any unsupplied param gets a dummy UUID
        placeholder substituted by the caller before use."""
        path = self.path
        for name, value in ids.items():
            placeholder = "{" + name + "}"
            path = path.replace(placeholder, str(value))
        return path


# ---------------------------------------------------------------------------
# The registry — 62 functional /api/v1 routes (41 + 5 lifecycle + 9 app shell +
# 6 team/filters/exports + 1 card feed); the /_debug/error route exists only
# when APP_DEBUG=true and is absent under test.
# ---------------------------------------------------------------------------

REGISTRY: tuple[EndpointSpec, ...] = (
    # ---- identity / auth ----
    EndpointSpec("POST", f"{API}/auth/login", "auth.login", auth=PUBLIC),
    EndpointSpec("POST", f"{API}/auth/refresh", "auth.refresh", auth=PUBLIC),
    # Phase 11: non-secret client flags (DEMO_MODE banner) — public, body-less GET.
    EndpointSpec("GET", f"{API}/public/config", "public.config", auth=PUBLIC),
    EndpointSpec("GET", f"{API}/me", "identity.me", auth=AUTH),
    # Phase 12 follow-ups (account recovery).  Public: forgot / reset / verify /
    # invite peek / accept (tokens in the body; the peek's in the path).
    EndpointSpec("POST", f"{API}/auth/forgot-password", "auth.forgot_password", auth=PUBLIC),
    EndpointSpec("POST", f"{API}/auth/reset-password", "auth.reset_password", auth=PUBLIC),
    EndpointSpec("POST", f"{API}/auth/verify-email", "auth.verify_email", auth=PUBLIC),
    EndpointSpec("GET", f"{API}/auth/invite/{{token}}", "auth.peek_invite", auth=PUBLIC),
    EndpointSpec("POST", f"{API}/auth/accept-invite", "auth.accept_invite", auth=PUBLIC),
    EndpointSpec("POST", f"{API}/auth/change-password", "auth.change_password", auth=AUTH),
    EndpointSpec("POST", f"{API}/auth/send-verification", "auth.send_verification", auth=AUTH),
    EndpointSpec(
        "POST",
        f"{API}/users/invite",
        "identity.invite_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/users/{{user_id}}/invite/resend",
        "identity.resend_invite",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/users/{{user_id}}/invite/revoke",
        "identity.revoke_invite",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/users",
        "identity.list_users",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
        collection=True,
    ),
    EndpointSpec(
        "GET",
        f"{API}/users/{{user_id}}",
        "identity.get_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # Known limits (organisation-aware login): the org's login code, read-only.
    EndpointSpec(
        "GET",
        f"{API}/organization",
        "identity.get_organization",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # Product gap 3: user administration — users.read AND the finance_admin role.
    EndpointSpec(
        "POST",
        f"{API}/users",
        "identity.create_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "PATCH",
        f"{API}/users/{{user_id}}",
        "identity.update_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/users/{{user_id}}/deactivate",
        "identity.deactivate_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/users/{{user_id}}/reactivate",
        "identity.reactivate_user",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # Known limits (AIR-04): the traveller grade ladder for the Users admin page.
    EndpointSpec(
        "GET",
        f"{API}/grades",
        "identity.list_grades",
        auth=PERM,
        permission="users.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # ---- travel / trips ----
    EndpointSpec(
        "POST",
        f"{API}/trips",
        "trips.create",
        auth=PERM,
        permission="trip.create.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/trips",
        "trips.list",
        auth=PERM,
        permission="trip.read.own",
        allowed_roles=ALL_ROLES,
        collection=True,
    ),
    EndpointSpec(
        "GET",
        f"{API}/trips/{{trip_id}}",
        "trips.get",
        auth=PERM,
        permission="trip.read.own",
        allowed_roles=ALL_ROLES,
    ),
    # PATCH is owner-only (C7-01): a reader who is not the traveler (e.g. the
    # traveler's manager) gets 403; approved/rejected are approval-workflow
    # outcomes and are never owner PATCH targets (403).
    EndpointSpec(
        "PATCH",
        f"{API}/trips/{{trip_id}}",
        "trips.transition",
        auth=PERM,
        permission="trip.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/trips/{{trip_id}}/segments",
        "trips.segments",
        auth=PERM,
        permission="trip.read.own",
        allowed_roles=ALL_ROLES,
    ),
    # Product gaps 4 / 8: withdraw a RETURNED trip (owner-only), mark an ended
    # approved trip completed (traveler or finance; a manager reader gets 403).
    EndpointSpec(
        "POST",
        f"{API}/trips/{{trip_id}}/withdraw",
        "trips.withdraw",
        auth=PERM,
        permission="trip.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/trips/{{trip_id}}/complete",
        "trips.complete",
        auth=PERM,
        permission="trip.read.own",
        allowed_roles=ALL_ROLES,
    ),
    # ---- expense ----
    EndpointSpec(
        "POST",
        f"{API}/expense-reports",
        "expense.create_report",
        auth=PERM,
        permission="expense.create.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
        idem_scope="expense_report.create",  # cross-user same-org key reuse -> 409
    ),
    EndpointSpec(
        "GET",
        f"{API}/expense-reports",
        "expense.list_reports",
        auth=PERM,
        permission="expense.read.own",
        allowed_roles=ALL_ROLES,
        collection=True,
    ),
    EndpointSpec(
        "GET",
        f"{API}/expense-reports/{{report_id}}",
        "expense.get_report",
        auth=PERM,
        permission="expense.read.own",
        allowed_roles=ALL_ROLES,
    ),
    # Report and entry PATCH are owner-only (C7-01): a non-owner reader gets 403.
    EndpointSpec(
        "PATCH",
        f"{API}/expense-reports/{{report_id}}",
        "expense.transition_report",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/expense-reports/{{report_id}}/entries",
        "expense.report_entries",
        auth=PERM,
        permission="expense.read.own",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "PATCH",
        f"{API}/expenses/{{entry_id}}",
        "expense.update_entry",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # C7R UAT report assembly: rename a draft / move entries between drafts.
    EndpointSpec(
        "PATCH",
        f"{API}/expense-reports/{{report_id}}/details",
        "expense.rename_report",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/expense-reports/{{report_id}}/entries/move",
        "expense.move_entries",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # Product gaps 1 / 4: finance-only simulated reimbursement (+ its queue) and
    # the owner's withdrawal of a RETURNED report.
    EndpointSpec(
        "POST",
        f"{API}/expense-reports/{{report_id}}/reimburse",
        "expense.reimburse_report",
        auth=PERM,
        permission="expense.reimburse.org",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/reimbursements/queue",
        "expense.reimbursement_queue",
        auth=PERM,
        permission="expense.reimburse.org",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/expense-reports/{{report_id}}/withdraw",
        "expense.withdraw_report",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # Product gap 2: add a server-computed mileage entry to an owned draft report.
    EndpointSpec(
        "POST",
        f"{API}/expense-reports/{{report_id}}/mileage",
        "expense.add_mileage",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # Follow-up (UI polish): the MIL-01 rate for the Add-mileage live estimate.
    EndpointSpec(
        "GET",
        f"{API}/mileage-rate",
        "expense.mileage_rate",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # UAT2: owner-only deletes (draft only; reports with approval history and
    # reviewed entries without a frozen snapshot -> 409) and attach-existing.
    EndpointSpec(
        "DELETE",
        f"{API}/expenses/{{entry_id}}",
        "expense.delete_entry",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "DELETE",
        f"{API}/expense-reports/{{report_id}}",
        "expense.delete_report",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "POST",
        f"{API}/expenses/{{entry_id}}/receipts",
        "expense.link_receipt",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # ---- ai / conversations ----
    EndpointSpec("POST", f"{API}/conversations", "ai.create_conversation", auth=AUTH),
    EndpointSpec(
        "POST",
        f"{API}/conversations/{{conversation_id}}/messages",
        "ai.send_message",
        auth=AUTH,
        idem_scope="conversation.message",
        streaming=True,
    ),
    EndpointSpec(
        "GET", f"{API}/conversations/{{conversation_id}}", "ai.get_conversation", auth=AUTH
    ),
    # ---- policy ----
    EndpointSpec(
        "POST",
        f"{API}/policy/query",
        "policy.query",
        auth=PERM,
        permission="policy.read",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "POST",
        f"{API}/policy/evaluate",
        "policy.evaluate",
        auth=PERM,
        permission="policy.read",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "POST",
        f"{API}/policies/{{policy_id}}/rules",
        "policy.create_rule",
        auth=PERM,
        permission="policy.manage",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/policies",
        "policy.get_active",
        auth=PERM,
        permission="policy.read",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "GET",
        f"{API}/policy-rules",
        "policy.list_rules",
        auth=PERM,
        permission="policy.read",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "PATCH",
        f"{API}/policy-rules/{{rule_id}}",
        "policy.patch_rule",
        auth=PERM,
        permission="policy.manage",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # Product gap 8: per-rule change history — finance admins and managers (role gate).
    EndpointSpec(
        "GET",
        f"{API}/policy/rules/{{rule_id}}/history",
        "policy.rule_history",
        auth=PERM,
        permission="policy.read",
        allowed_roles=frozenset({FINANCE, MANAGER}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    # ---- receipts ----
    EndpointSpec(
        "POST",
        f"{API}/receipts",
        "receipts.upload",
        auth=PERM,
        permission="expense.create.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
        idem_scope="receipt.upload",  # cross-user same-org key reuse -> 409
        multipart=True,
    ),
    # GET is readable by owner, owner's direct manager and finance (scope_query);
    # it mints the NFR-SEC4 short-lived image link. POST needs expense.create.own
    # and PATCH needs expense.update.own (finance_admin -> 403); PATCH stays
    # uploader-only (a non-uploader holding the permission gets 404).
    EndpointSpec(
        "GET",
        f"{API}/receipts/{{receipt_id}}",
        "receipts.get",
        auth=PERM,
        permission="expense.read.own",
        allowed_roles=ALL_ROLES,
    ),
    EndpointSpec(
        "PATCH",
        f"{API}/receipts/{{receipt_id}}",
        "receipts.correct",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # UAT2: the caller's attachable receipts (owner-only list, non-paginated,
    # capped at 50) and DELETE (uploader-only; reviewed evidence -> 409).
    EndpointSpec(
        "GET",
        f"{API}/receipts",
        "receipts.list_attachable",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    EndpointSpec(
        "DELETE",
        f"{API}/receipts/{{receipt_id}}",
        "receipts.delete",
        auth=PERM,
        permission="expense.update.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
    # ---- approvals ----
    # POST trigger gates (C7-03 / C7-13), checked before the agent runs: subject
    # outside the caller's read scope -> 404; caller owns the subject -> 403 (no
    # self-approval); approval already exists -> 409; subject not submitted
    # (report ``submitted`` / trip ``pending_approval``) -> 400
    # INVALID_SUBJECT_STATE. PATCH decide: the submitter deciding their own
    # approval -> 403.
    EndpointSpec(
        "POST",
        f"{API}/approvals",
        "approvals.trigger",
        auth=PERM,
        permission="approval.decide.reports",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/approvals",
        "approvals.list",
        auth=PERM,
        permission="approval.read",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
        collection=True,
    ),
    # Product gap 6: filter options (scoped counts / departments).
    EndpointSpec(
        "GET",
        f"{API}/approvals/exception-types",
        "approvals.exception_types",
        auth=PERM,
        permission="approval.read",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/approvals/departments",
        "approvals.departments",
        auth=PERM,
        permission="approval.read",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    # Product gap 8: finance-only CSV export (approval.read + expense.read.org).
    EndpointSpec(
        "GET",
        f"{API}/approvals/export.csv",
        "approvals.export_csv",
        auth=PERM,
        permission="expense.read.org",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    EndpointSpec(
        "GET",
        f"{API}/approvals/{{approval_id}}",
        "approvals.get",
        auth=PERM,
        permission="approval.read",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    EndpointSpec(
        "PATCH",
        f"{API}/approvals/{{approval_id}}",
        "approvals.decide",
        auth=PERM,
        permission="approval.decide.reports",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    # ---- app shell (product gap 3) ----
    # Notifications are the caller's OWN derived feed (any authenticated user;
    # scoping is in the service). Page envelope, but no filter/sort surface.
    EndpointSpec("GET", f"{API}/notifications", "notifications.list", auth=AUTH),
    EndpointSpec("GET", f"{API}/notifications/unread-count", "notifications.unread", auth=AUTH),
    EndpointSpec("POST", f"{API}/notifications/read-all", "notifications.read_all", auth=AUTH),
    EndpointSpec(
        "POST",
        f"{API}/notifications/{{notification_id}}/read",
        "notifications.read_one",
        auth=AUTH,
    ),
    # Global search: any authenticated user; each group is read-scoped.
    EndpointSpec("GET", f"{API}/search", "search.global", auth=AUTH),
    # Phase 11: read-only rate-limiter status — the finance_admin role only.
    EndpointSpec(
        "GET",
        f"{API}/ops/rate-limits",
        "ops.rate_limits",
        auth=PERM,
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # Phase 12: read-only scheduled demo reset status — the finance_admin role only.
    EndpointSpec(
        "GET",
        f"{API}/ops/demo-reset",
        "ops.demo_reset",
        auth=PERM,
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # ---- analytics ----
    EndpointSpec("GET", f"{API}/analytics/overview", "analytics.overview", auth=AUTH),
    # ---- audit ----
    EndpointSpec(
        "GET",
        f"{API}/audit",
        "audit.list",
        auth=PERM,
        permission="audit.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
        collection=True,
    ),
    # Product gap 8: finance-only CSV export of the audit log.
    EndpointSpec(
        "GET",
        f"{API}/audit/export.csv",
        "audit.export_csv",
        auth=PERM,
        permission="audit.read",
        allowed_roles=frozenset({FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE, MANAGER}),
    ),
    # ---- team (Product gap 5) ----
    # Gated on ANY of expense.read.reports (manager) / expense.read.org (finance);
    # a member outside the caller's team -> 404.
    EndpointSpec(
        "GET",
        f"{API}/team/members",
        "team.list_members",
        auth=PERM,
        permission="expense.read.reports|expense.read.org",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
        collection=True,
    ),
    EndpointSpec(
        "GET",
        f"{API}/team/members/{{user_id}}",
        "team.get_member",
        auth=PERM,
        permission="expense.read.reports|expense.read.org",
        allowed_roles=frozenset({MANAGER, FINANCE}),
        forbidden_roles=frozenset({EMPLOYEE}),
    ),
    # ---- cards (product gap 8) ----
    # The caller's OWN card feed (owner-only for every role; Page envelope with
    # status / date filters).  Same gate as the Receipts area it lives in.
    EndpointSpec(
        "GET",
        f"{API}/card-transactions",
        "cards.list",
        auth=PERM,
        permission="expense.create.own",
        allowed_roles=frozenset({EMPLOYEE, MANAGER}),
        forbidden_roles=frozenset({FINANCE}),
    ),
)


# Convenience views -----------------------------------------------------------

PROTECTED = tuple(e for e in REGISTRY if e.auth != PUBLIC)
PUBLIC_ENDPOINTS = tuple(e for e in REGISTRY if e.auth == PUBLIC)
COLLECTIONS = tuple(e for e in REGISTRY if e.collection)
IDEMPOTENT = tuple(e for e in REGISTRY if e.idem_scope is not None)


def wrong_role_cases() -> list[tuple[EndpointSpec, str]]:
    """Flatten (endpoint, forbidden_role) pairs for the RBAC 403 matrix."""
    pairs: list[tuple[EndpointSpec, str]] = []
    for e in REGISTRY:
        for role in sorted(e.forbidden_roles):
            pairs.append((e, role))
    return pairs
