# Phase 6 · P6-T3 — Database ↔ APIs

## Goal

Prove record-level scoping holds across all modules and that the lineage chain
(receipt → expense → expense_report → trip → trip_segment) is intact; validate
cross-role access tests pass and a lineage query returns the full chain.

## What was done

- **Adversarial cross-role suite** (42 tests, `test_t3_cross_role_scoping.py`):
  employee own-only; manager direct-reports-only; finance org-wide; cross-tenant
  always 404. 403/404 convention confirmed consistent across all modules —
  out-of-scope records return 404 (no existence leak), wrong-role surfaces return
  403, list endpoints return 200 with filtered results.

- **Full lineage chain proved** (`test_t3_lineage.py`, 9 tests): receipt →
  expense → expense_report → trip → trip_segment, with the smart-match cross-link
  (`expenses.trip_segment_id`) and card_transaction grounding. Zero dangling
  non-null FKs across all tables. Expected NULLs documented (unmatched
  receipt `expense_id`, untied report `trip_id`, unmatched expense
  `trip_segment_id`).

- **T2 carries resolved** (`test_t3_t2_carries.py`, 11 tests): context_ref
  cross-owner blocked (404 via record-level filter); policy RAG SQL confirmed
  org-scoped (structural assertion + policy catalog isolation test).

- **Production fix — service.py**: `validate_context_ref_id()` tightened from
  org-only (`organization_id: uuid.UUID`, bare WHERE clause) to record-level
  (`user: User`, `scope_query()`). T3-discovered T2 bug: origin P6-T2 passed
  `organization_id=current_user.organization_id` at the call site, so the
  record-level check never ran — employee A could silently reference employee
  B's trip/report when opening a conversation.

- **Call-site fix — router.py**: `user=current_user` (was
  `organization_id=current_user.organization_id`). Pure call-site arg change;
  no logic change in the function beyond what service.py now does. This
  qualifies the P6-T2 commit's "record-level validation" claim: the intended
  check was correct but the wrong kwarg prevented it from executing.

- **Dockerfile.frontend fix** (libc6-compat, bundled in this commit): Alpine
  uses musl; the SWC binary in the lockfile requires the GNU `ld-linux-x86-64.so.2`.
  `libc6-compat` added to both `deps` and `builder` stages. Fix was present in
  the local P6-T2 commit but never pushed; bundled here to restore CI green.

## Key decisions

- **`scope_query()` reuses existing enforcement** — `validate_context_ref_id()`
  uses the identical `scope_query()` from `backend.app.core.rbac` that every
  GET/{id} endpoint uses. No new parallel auth path; the filter cannot drift from
  live enforcement.

- **Manager approval_queue org-wide visibility** — `_build_manager_overview`
  builds `approval_queue` org-wide (explicit code comment: "managers approve
  org-wide") while the PRD requires direct-reports-only analytics. `PATCH
  /approvals` IS correctly team-scoped. The PRD-vs-code-comment tension is
  documented with `pytest.xfail` (reason: "ANALYTICS_FLAG: Manager B sees
  Employee A's approval in org-wide queue (analytics/service.py
  _build_manager_overview: 'managers approve org-wide'). PRD says team-only
  analytics. Flagged for an owner decision — not auto-fixed in T3."). Fix-vs-
  intended deferred.

- **router.py re-touched from P6-T2** — the call-site fix is a T3-discovered
  correction to P6-T2 logic. Included in this commit to keep the fix and the
  tests that verify it in the same commit.

## Verification

62 tests: 42 cross-role, 9 lineage, 11 T2-carries — all green (or xfail for the
documented analytics finding). Drift guard empty (`database/` clean, no seed
edit, `ai_agents/` imports no `backend`). ruff + mypy clean on all touched files.
Tests self-skip cleanly when Postgres is unreachable.

## Carried gaps (out of P6-T3 scope)

- `GET /receipts/{id}` not implemented → receipt cross-owner proven via PATCH only
- `GET /audit` not implemented (no audit router)
- `policy_qa` entity-scoped conversation context → 422 (deferred)
- Manager approval_queue org-wide visibility → fix-vs-intended pending an owner decision
- Loosely-typed backend schemas; NFR-SEC4 image serving; boto3 upload gap → T4/T5/infra
