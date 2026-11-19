# Phase 7 · P7-T2 — API Contract Tests

**Date:** 2026-06-19  
**Branch:** test/api  
**Track:** API contract tests

## Goal

Build a parametrized API contract matrix that proves — centrally and uniformly — the
correctness of every live `/api/v1` endpoint across six contract dimensions:

- **Coverage** — registry == introspected live route table; 32/32 endpoints, no skips
- **Auth** — every protected endpoint: 401 on missing token, 401 on garbage token, never 500
- **RBAC** — every permission-gated endpoint: valid token + forbidden role → 403 + envelope
- **Validation** — malformed body → 422; bad enum → 422
- **Pagination / filter** — clamped page/size, empty-page 200, 400 on bad filter/sort field
- **Idempotency** — same Idempotency-Key → replay (no double-write); cross-org isolation holds
- **State semantics** — illegal transitions → 409; required-reason enforcement; optimistic locking

## What was built

### `backend/tests/api/endpoints.py` — endpoint registry (single source of truth)

32 `EndpointSpec` entries: method, path template, auth class (`PUBLIC` / `AUTH` / `PERM`),
required permission key, authorized/forbidden roles, collection flag, idempotency scope, streaming flag,
multipart flag. `wrong_role_cases()` yields every `(EndpointSpec, forbidden_role)` pair for the
parametrized 403 matrix.

### `backend/tests/api/conftest.py` — additive fixtures

Composes `backend/tests/conftest.py` — no shared fixtures are redefined.

- `_persistent_rbac_catalog` (session-scoped, autouse): seeds the RBAC catalog once (committed)
  before any test. Eliminates the pre-existing `uq_roles_key` UniqueViolation flake —
  per-test `seed_rbac_catalog()` calls become get-or-create no-ops. See QA Findings.
- `anon_client`: `TestClient` with no Authorization header (`raise_server_exceptions=False`).
- `role_clients`: `{"employee": …, "manager": …, "finance_admin": …}` — real-JWT org-A clients.
- `local_providers`: monkeypatches all four provider deps + `runtime.get_llm_provider` +
  `build_orchestrator.cache_clear()` for deterministic receipt/AI/streaming paths.

### `backend/tests/api/_helpers.py` — central assertion helpers

- `assert_error_envelope(resp, expected_status)`: asserts `{"error":{code,message,correlation_id}}`
  top-level shape; non-empty fields; `X-Correlation-ID` header matches body CID. Returns CID.
- `parse_sse(text)` / `sse_done_message_id(text)`: SSE frame parser; extracts `message_id` from
  the `done` frame for streaming idempotency assertions.
- `dummy_uuid()`: generates a fresh UUID string for path parameter substitution.

### `test_contract_matrix.py` — 113 tests

Central uniformity proof.

- `test_registry_covers_every_live_route()`: introspects `fastapi_app.routes`, excludes
  `/_debug/error` (absent when `APP_DEBUG=False`), asserts `registry == live`, count == 32.
- `test_registry_has_no_duplicate_keys()`: asserts uniqueness of `(method, path)` keys.
- AUTH parametrized over 30 PROTECTED endpoints: no-token → 401, garbage token → 401, never 500.
- PUBLIC endpoints: empty body → 422 (not 401), proves no auth gate.
- `test_login_wrong_credentials_is_401_envelope()`: domain 401 (bad creds) also returns the
  standard envelope, distinct from the auth-gate 401.
- RBAC parametrized over `wrong_role_cases()`: valid token + forbidden role → 403 + envelope.
  `_dispatch()` sends `json={}` — auth/permission dependencies resolve before body validation,
  so no 422-first false negative.

### `test_validation.py` — 16 tests

Malformed body → 422 with `VALIDATION_ERROR` code and generic message `"Request validation
failed."` (anti-leak: field detail is server-side logged only). 14 parametrized cases covering
trips, expense reports, AI messages, policy, receipts, approvals. Also: missing multipart file →
422; non-UUID path param → 422.

### `test_rbac_scoping.py` — 7 tests

Info-leak convention: out-of-scope GET-by-id → 404 (not 200, not 403).

- Positive: employee → own ✓, manager → self + direct reports ✓, finance → org-wide ✓.
- Negative: employee/manager → stranger (same org) → 404; cross-tenant → 404.
- `test_nonexistent_and_out_of_scope_are_indistinguishable()`: both give the same 404 status,
  preventing enumeration via status-code discrimination.
- `test_user_read_cross_tenant_is_404_even_for_finance()`: org-wide ≠ all-orgs.
- `test_conversation_read_is_owner_only()`: non-owner in same org → 404.

### `test_pagination.py` — 5 tests

Covers all 4 paginated collections (`/users`, `/trips`, `/expense-reports`, `/approvals`).

- First page (size=2) + last page: consistent totals.
- Out-of-range page: clamped → empty items + 200 (NOT rejected).
- `page<1`: clamped to 1.
- Bad filter/sort field → 400 (deny-by-default; NOT 422).
- Valid sort + valid filter → 200.
- `test_trips_sort_is_actually_honored()`: creates trips with distinct `start_date`, asserts
  asc/desc ordering is real.

### `test_idempotency.py` — 8 tests

`local_providers` fixture used for receipt/AI paths.

- Same key + same body → replay (id1 == id2, row count == 1).
- Different keys → 2 distinct rows.
- No Idempotency-Key → every call writes (no dedup).
- Same key + different body → **replays first entity** (no body hash, not 409) — divergence flagged.
- Same org, different caller + same key → replays first caller's entity — divergence flagged.
- Cross-org, same key string → different entity ids (org isolation holds).
- Receipt upload (multipart): same key → single `Receipt` row.
- SSE streaming `POST /conversations/{id}/messages`: same key → same `message_id` in `done` frame;
  `ConversationMessage` user-role count == 1.

### `test_state_semantics.py` — 8 tests

- Illegal trip state transition (draft → approved) → 409, state unchanged.
- Illegal expense-report transition (draft → approved) → 409, state unchanged.
- `decide_approval` without required reason (`reject`/`request_changes`) → **400** (divergence flagged).
- `decide_approval` with reason → 200 (positive control).
- Stale `If-Match` on `PATCH /expense-reports/{id}` → 409, version unchanged.
- Current `If-Match` → 200, `version == prior + 1` (proves CAS is real).
- Stale `If-Match` on `PATCH /approvals/{id}` → 409.

**Total: 157 tests**

## Reconcile findings

| Topic | Briefed | Actual |
|---|---|---|
| Error envelope | Flat `{code,message,correlation_id}` | **Nested** `{"error":{code,message,correlation_id}}` + `X-Correlation-ID` header (authoritative per P3-T4) |
| Idempotency key | `(organization_id, scope, idempotency_key)` | Confirmed — no body hash, no caller-id scoping |
| Pagination out-of-range | Implicit rejection | Clamped → empty 200 |
| Bad filter/sort field | 422 | **400** (deny-by-default `BadRequestError`) |
| Approval required-reason | 422 | **400** (`BadRequestError` inline in `decide_approval`) |
| LLM_PROVIDER | `mock` (spec) | **`local`** — `mock` invalid; `Settings` allows only `local\|anthropic` |
| `/policies` / `/policy-rules` | Paginated | **Non-paginated** (intentional) |

## Divergences flagged (actual behavior asserted; no production changes)

1. **Idempotency same-key + different-body replays (not 409)** — design says conflict; live
   replays the first entity silently. No body hash stored or checked.
   `test_same_key_different_body_replays_first_no_409`.

2. **Idempotency scoped to org, not caller** — same-org, different-caller users sharing an
   Idempotency-Key replay the first caller's entity. Potential cross-user exposure within the
   same organization. `test_same_org_different_caller_replays_first_callers_entity`.

3. **Validation body is generic** — `"Request validation failed."` returned (anti-leak); no
   field names or reasons in the response body. Tests assert 422 + `VALIDATION_ERROR` code.

4. **Bad filter/sort field → 400, not 422** — deny-by-default parser raises `BadRequestError`,
   not a Pydantic `ValidationError`.

5. **Approval required-reason → 400, not 422** — inline `BadRequestError` in `decide_approval`
   service; inconsistent with brief expectation of 422. **Cross-finding note:** independently
   confirmed by P7-T1 (`approval/service.py:410–411`). High-confidence real bug; Phase 8
   fix candidate.

6. **`/policies` and `/policy-rules` intentionally non-paginated** — GET /policies returns a
   single active policy document; GET /policy-rules returns a flat filterable list. Pagination
   uniformity does not apply.

## Key decisions

- **Parametrized contract matrix as single source of truth** — `endpoints.py` registry drives
  the 32/32 coverage gate, the 401/403 parametrization, and the wrong-role matrix from one
  definition. Adding a new endpoint requires only an `EndpointSpec` entry; all matrix tests
  automatically cover it.

- **Additive conftest (session-scoped autouse `_persistent_rbac_catalog`)** — mitigates the
  pre-existing `uq_roles_key` UniqueViolation flake within this package. Shared conftest not
  modified (out of scope). Per-test `seed_rbac_catalog()` calls become get-or-create no-ops.

- **Divergences asserted as actual behavior, not fixed** — production code changes are out of
  scope. Divergences are documented in the FSR and flagged for follow-up.

- **`LLM_PROVIDER=local`** — the spec's `mock` value is invalid per `Settings`; `local`
  provides deterministic AI responses.

- **`_dispatch()` sends `json={}`** — FastAPI evaluates auth/permission dependencies before
  Pydantic body validation, so an empty body surfaces auth/RBAC failures without a 422-first
  false negative.

- **Coverage via 32/32 gate** — `pytest-cov` / `coverage.py` not installed in the venv.
  Router-layer exercise proven by the introspection coverage gate instead.

## Verification

**Checks — all clean:**

- Check 1 (real secrets): CLEAN — no AWS keys, OpenAI keys, or real passwords found.
- Check 2 (secret-shaped strings): REVIEWED — all synthetic. `"Bearer not-a-real-jwt.token.value"`:
  intentional garbage token for negative test. `"password": "wrong"`: wrong-credentials probe body.
  Idempotency-Key values (`exprep-key-AAA`, `k-1`, `k-2`, etc.): test-only strings. No real JWTs.
- Check 3 (`ruff check`): 0 issues. (5 import-ordering issues auto-fixed during development.)
- Check 4 (`mypy`): 0 issues in 10 source files.
- Check 5 (`pytest backend/tests/api -q`): **157 passed** in 3.39 s.

**Tier 1:** 157/157 green; ruff clean; mypy clean; 32/32 endpoint coverage gate passes.

**Tier 2 (all mutations confirmed; all reverted; tree clean):**
1. `scope_query` employee branch loosened → `test_trip_scoping_positive_and_leak` **FAILED** ✔ — leak caught.
2. `idempotency_lookup` replay disabled → `test_same_key_same_body_replays_no_double_write` + receipt variant **FAILED** ✔ — double-write caught.
3. `require_permission` check bypassed → `test_wrong_role_is_forbidden` (6+ parametrized cases) **FAILED** ✔ — 403 bypass caught.

Production tree after Tier 2: clean (`git status` shows no tracked changes).

## QA Findings

### 1. Pre-existing foundation flake — `uq_roles_key` UniqueViolation (high)

**Root cause:** `seed_rbac_catalog()` calls `session.commit()` under
`join_transaction_mode="create_savepoint"`, which releases a savepoint but does NOT release
the outer connection. The connection holds the `uq_roles_key` page lock. A subsequent test's
get-or-create INSERT in the same process can collide at SETUP.

**Scope:** pre-existing — reproducible with `test_rbac.py`, `test_record_scoping.py`,
`test_t3_cross_role_scoping.py` (live Postgres only; hermetic CI skips `db` tests).
**NOT caused by P7-T2.** Not fixed here — shared conftest is out of scope.

**Mitigation (within P7-T2 only):** `_persistent_rbac_catalog` session-scoped autouse fixture
pre-seeds the catalog permanently before any test. Per-test calls become get-or-create no-ops
(no INSERT, no unique-key contention). Single-run determinism restored within this package.

**Needs:** a separate hardening pass on `backend/tests/conftest.py` to fix the root cause for
all Phase 7 test suites.

### Cross-finding note

`approval/service.py:410–411` (required-reason returns 400 not 422) was independently flagged
by both **P7-T1** (unit tests) and **P7-T2** (API contract tests). High confidence — real bug.
Phase 8 fix candidate.
