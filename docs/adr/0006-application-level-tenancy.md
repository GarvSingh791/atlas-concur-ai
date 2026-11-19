# 0006 — Application-level tenant isolation, without row-level security

- **Status:** Accepted
- **Date:** 2026-06-12 (record scoping, phase P2-T4); organisation-aware login added 2026-11-14 (migration 0022)
- **Decider:** Garv Singh Saini

## Context

The data model is multi-tenant: every root business table carries `organization_id`, and since migration 0022 the same email can exist in two organisations, with an optional organisation code at login. The demo itself runs one organisation, but tests run two, and the controls must hold as if a second tenant were live.

Within a tenant there is a second layer of scoping by role: an employee sees their own records, a manager also sees direct reports' records, a finance admin sees the whole organisation.

PostgreSQL offers row-level security (RLS) that could enforce the tenant filter in the database. The question was whether to rely on it or on the application.

## Decision

Enforce tenancy and role scope **in the application, through one choke point**:

- `backend/app/core/rbac.py::scope_query` takes a statement, the caller, the ownership column and the tenant column. It **always** applies `org_column == caller.organization_id` first, even for finance admins, then narrows by role (employee → own, manager → own plus direct reports one level deep, finance admin → organisation). An unknown role raises 403.
- Writes use `require_owner` / `owner_scope_query`: owner-only for every role. A record the caller cannot read is a 404; a readable but not-owned one is a 403.
- Child tables (`trip_segments`, `approval_steps`, `policy_rules`, `policy_embeddings`, `conversation_messages`, `notification_reads`) do not repeat `organization_id`; they are reached through their scoped parent.
- Retrieval joins policy chunks to their organisation (`ai_agents/tools/policy_retrieval.py`), so RAG cannot return another tenant's clauses.
- Authentication loads the user by `(id, organization_id)` from the token claims, so a token cannot be replayed into another organisation.

No PostgreSQL RLS policies are defined. The schema keeps `organization_id` on every root table so RLS could be added later without a schema change.

## Alternatives considered

| Option | Why not (for now) |
|---|---|
| PostgreSQL row-level security keyed on a session variable (`SET app.org_id`) | Strong defence in depth, but every connection must set the variable reliably (including connection reuse from the pool, migrations, seeds and operational scripts that legitimately work across organisations), and role scoping (direct reports) would still live in the application. Debugging silent empty results under RLS also costs time. Deferred, not rejected. |
| Schema-per-tenant or database-per-tenant | Isolation by construction, but migrations, the shared RAG corpus pattern and operations multiply per tenant. Out of proportion for a demo with one live organisation. |
| Scoping written ad hoc in each query | The usual source of IDOR bugs. A single function is reviewable and testable. |

## Consequences

**Positive**

- One function to review and test; the tenant filter cannot be forgotten for a finance admin because it is applied before the role branch.
- Role scoping and tenant scoping are expressed in the same place.
- Tested at three levels: unit, DB-backed with two organisations, and per-endpoint in the contract matrix; the RAG eval probes 27 cross-tenant cases and requires zero leaks.

**Negative / trade-offs**

- **No database backstop.** A new query that bypasses `scope_query` would not be caught by PostgreSQL. This is recorded as a residual risk in the [security model](../design/security-model.md#residual-risks).
- Operational scripts that span organisations (demo reset, cleanup) need their own explicit guards (for example `--org "Atlas Demo Corp"` and a refusal on cross-tenant references).

## Evidence

- Code: [`backend/app/core/rbac.py`](../../backend/app/core/rbac.py), [`database/models/mixins.py`](../../database/models/mixins.py) (`TenantMixin`), `backend/app/core/security.py::get_current_user`.
- Tests: `backend/tests/test_record_scoping.py` (two organisations), `backend/tests/unit/test_unit_rbac_scoping.py`, `backend/tests/api/test_rbac_scoping.py`, `backend/tests/test_c7_g1_rbac_write_scope.py`, `backend/tests/test_kl_org_scoped_login_db.py`, and the RAG tenant probe `test_rag_org_scoping_zero_cross_tenant` in `backend/tests/eval/test_eval_db_retrieval.py`.
- Phase log: [`P2-T4_rbac_record_scoping.md`](../phase_logs/P2-T4_rbac_record_scoping.md).
