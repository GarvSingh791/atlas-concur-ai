# P7-T1 — Backend Unit Tests

**Date:** 2026-06-19  
**Branch:** test/unit  
**Track:** backend unit tests  
**Status:** Complete — uncommitted, awaiting commit

---

## What was built

Six new files under `backend/tests/unit/` (no `__init__.py` needed; parent conftest
inherited via pytest scoping). 102 tests total (65 functions × `@pytest.mark.parametrize`).

| File | Tests | Coverage target |
|---|---|---|
| `test_unit_rbac_scoping.py` | 16 | `core.rbac` 91% |
| `test_unit_policy_service.py` | 13 | `modules.policy.service` 98% |
| `test_unit_state_transitions.py` | 15 | transition paths in expense + travel services |
| `test_unit_concurrency_idempotency.py` | 26 | `core.concurrency` 100%, `core.idempotency` 100% |
| `test_unit_validation_constraints.py` | 14 | `core.validation` 100% |
| `test_unit_approval_determinism.py` | 18 | `modules.approval.risk` 80%, `decide_approval` path fully covered |

---

## Scope decision

**"Backend gaps only"** (owner decision).

The AI-layer P7-T1 items (policy-rule evaluation, smart-match, guardrails, all 5 tools)
live in `ai_agents/`, not the backend, and are already covered by ~100 hermetic unit
tests in `ai_agents/tests/` (`test_tool_*`, `test_role_bound_check`,
`test_citation_contract`, `test_rag_retrieval`, `test_expense_agent`). Those tests are
cited and deferred — not duplicated.

`derive_risk_level` (approval risk) is cited to the existing
`backend/tests/test_approval_risk.py` (7 tests) and not re-tested.

---

## Import paths reconciled

- **RBAC**: `backend.app.core.rbac` — `scope_query` is called in the **service layer**
  (not router, not data-access layer); applies `org_column == user.organization_id`
  unconditionally first, then narrows by role.
- **Concurrency / idempotency / validation**: `backend.app.core.{concurrency,idempotency,validation}`.
- **Transitions**: `backend.app.modules.expense.service` (transition_report),
  `backend.app.modules.travel.service` (transition_trip).
- **Approval**: `backend.app.modules.approval.service` (decide_approval) +
  `backend.app.modules.approval.risk` (derive_risk_level — cited only).
- **Policy org-JOIN**: `backend.app.modules.policy.service:106-113` — `update_rule_status`
  JOINs `PolicyRule→Policy` and filters `Policy.organization_id` so cross-org rule_id
  returns `NotFoundError`, no data leak.

---

## Key decisions

- **Sync SQLAlchemy stack inherited from foundation** — no asyncio, no pytest-asyncio;
  `join_transaction_mode="create_savepoint"` for rollback-safe test sessions.
- **Real JWT role clients** (not mock auth): `create_access_token(user.id, user.organization_id)`.
- **Live-DB self-skip**: `db_session` calls `pytest.skip("Postgres not reachable")` when DB
  is unreachable — negative control confirmed (green = real, not false-green-from-skips).
- **No-LLM proof pattern** (`assert not hasattr(module, "get_llm_provider")`) mirrors
  `ai_agents/tests/test_policy_agent.py:1032` — proves deterministic modules never import
  the provider factory.
- **Tests assert current behavior** for all behavioral discrepancies found during RECONCILE
  (see QA findings). Nothing was fixed; deviations are flagged.

---

## Self-verification

### Tier 1

| Check | Result |
|---|---|
| `pytest backend/tests/unit` | 102 passed, 0 failed, 0 skipped (3 consecutive runs) |
| Live-DB confirmed running | Postgres healthy; negative-control (unreachable DB) shows tests SKIP |
| `ruff check backend/tests/unit` | All checks passed |
| `ruff format --check backend/tests/unit` | 6 files already formatted |
| `mypy backend/tests/unit` | Success: no issues found in 6 source files |

**Coverage delta** (measured temporarily with pytest-cov, then uninstalled):
`core.concurrency` 100% · `core.idempotency` 100% · `core.validation` 100% ·
`core.rbac` 91% · `modules.policy.service` 98% · `modules.approval.risk` 80% ·
Transition/decision paths in expense, travel, and approval services fully covered
(only uncovered lines are the shared `entity is None → 404` API branches = P7-T2 scope).

### Tier 2 — mutation checks

All 5 mutations were applied, confirmed to break the relevant tests, then reverted.
Production files verified byte-identical via sha256 after every revert.
`git diff` at task end: **0 modified tracked files**.

| ID | Mutation | Test that bit | Result |
|---|---|---|---|
| a | Commented out manager `.in_(visible_ids)` in `scope_query` (rbac.py) | `test_manager_sees_self_plus_direct_reports_one_level` | FAILED ✓ |
| b | Commented out `org_column == user.organization_id` tenant filter (rbac.py) | `test_cross_tenant_row_never_returned[finance]` | FAILED ✓ |
| c | Disabled `not in LEGAL_TRANSITIONS` check in `transition_report` (expense/service.py) | `test_expense_illegal_transition_raises_conflict_no_audit` (all 5 params) | FAILED ✓ |
| d | Disabled inline required-reason check in `decide_approval` (approval/service.py) | `test_decide_reject_without_reason_raises_bad_request` (all 4 params) | FAILED ✓ |
| e | Disabled `not in LEGAL_TRANSITIONS` check in `transition_trip` (travel/service.py) | `test_trip_illegal_transition_raises_conflict_no_audit` (all 5 params) | FAILED ✓ |

---

## QA findings flagged for follow-up

Tests assert **current behavior** — nothing was fixed. These require an owner decision.

**QA-1** · `backend/app/modules/expense/service.py:374-378`  
Phase-4 transition gate is **still active at Phase 7**: `submitted→{approved,rejected,reimbursed}`
via `transition_report` (PATCH endpoint) always raises `ForbiddenError` (`_PHASE4_GATED_TARGETS`).
*Question: Is this intentional-final (status only via the Approval module) or a stale gate that
should now be lifted?*

**QA-2** · `backend/app/modules/travel/service.py:280`  
`transition_trip` uses a manual `trip.version = trip.version + 1` with **no optimistic CAS**
(`guarded_version_update`), unlike `ExpenseReport` and `Approval`. Trips have no concurrency
protection on status change.

**QA-3** · `backend/app/modules/approval/service.py:410-411`  
`decide_approval` inlines the missing-reason check and raises `BadRequestError` (400), whereas
`validate_required_reason` in `core/validation.py` raises `UnprocessableEntityError` (422) for
the same semantic. Same condition, two different status codes.

**QA-4** · `backend/app/modules/approval/service.py:410` vs `backend/app/core/validation.py:22`  
`decide_approval`'s guard is `not reason` (falsiness only) — a whitespace-only reason like
`"   "` is **accepted** for reject/request_changes. `validate_required_reason` uses
`not reason or not reason.strip()` and would reject it. Pinned by
`test_decide_reject_whitespace_reason_currently_accepted` (assertion must flip if the guard
is later tightened).

---

## Pre-existing caveat (not introduced by P7-T1)

The full-repo suite (`backend/tests` + `ai_agents/tests` combined) has **order-dependent
test-isolation failures**: rows committed past the savepoint rollback create
`ai_decision_logs` FK violations; a `DROP SCHEMA` in another test leaks across test files.
These failures reproduce with the `backend/tests/unit/` directory entirely excluded.
Warrants a separate test-infra hardening pass.

---

## Status

Complete; the QA findings above are the open items.
