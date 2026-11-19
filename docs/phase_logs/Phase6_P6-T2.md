# Phase 6 · P6-T2 — Backend ↔ AI Agents

## Goal

Prove the delegation seam between `backend/app/modules/ai` and `ai_agents` holds
for all six agent paths (`orchestrator`, `travel`, `expense`, `policy`,
`approval`, `receipt_analysis`). The gate: each path writes a committed, durable,
correctly-scoped `ai_decision_logs` row via correct one-way delegation with
server-derived scoped context.

## What was done

**Delegation chain verified for all 6 paths:**
thin AI layer → orchestrator → agent → guardrails/reflection → flush → endpoint
commit. All six paths (`orchestrator`, `travel`, `expense`, `policy` llm +
deterministic, `approval`, `receipt_analysis`) confirmed to write durable,
correctly-scoped `ai_decision_logs` rows (live DB, separate-connection query).

**FIX-1 — thread `context_ref_id` into the agent Task payload:**
`send_message` (`router.py`) now injects `context_ref_id` from the stored
`Conversation` row into the Task payload when non-None. Added nil-UUID guard
(`is not None` replaces falsy check to handle `00000000-...` correctly).
Agents receive the entity UUID as opaque data in `Task.payload`; no
`ai_agents/` change required (layering law intact).

**FIX-2 — `validate_context_ref_id()` at conversation create:**
New helper in `service.py` validates that any `context_ref_id` supplied at
`POST /conversations` belongs to the caller's organization:
- `context_type=general` + any `context_ref_id` → 422 (meaningless combination).
- `context_type=travel_planning` → org-scoped `Trip` lookup; 404 if not found.
- `context_type=expense` → org-scoped `ExpenseReport` lookup; 404 if not found.
- Other types with `context_ref_id` → 422 (explicit, unsupported).

**Validation depth — ORG-ONLY (explicit limitation):** The helper validates
org-membership of the referenced entity (`Trip.organization_id == org_id`), not
per-owner record ownership. Intra-org references by employees to other employees'
records are not rejected. This is the correct floor for the AI assistant context
(employees query the assistant about org-level data), but the intra-org
user-level ownership case is flagged for T3's adversarial access suite.

**GAP-2 — Citation SSE serialization fix:**
`streaming.py` was passing `Citation` dataclass objects to `json.dumps`. Changed
to `[c.to_dict() for c in result.citations]`. Same fix applied proactively to
`policy/router.py` (`query_policy` endpoint, same pattern).

**GAP-1 (sibling-module deviation, documented) — policy eval-context hardening:**
`policy/router.py` (`evaluate_policy`) previously sent `context` as a nested dict.
Changed to flatten allowed keys via `_ALLOWED_EVAL_CONTEXT_KEYS` whitelist
(`frozenset(EvalItem.__dataclass_fields__)`) and moved server-authoritative keys
(`subject_type`, `subject_id`) last. Security: prevents mass-assignment injection
of arbitrary keys into the Task payload. **Verdict-neutral:** `subject_type` and
`subject_id` are not `EvalItem` fields (never in the whitelist) and do not flow
into `PolicyEvalTool.evaluate()`; rule verdicts are determined solely by `EvalItem`
field values and org-scoped active rules.

**Tests added (`test_conversations_api.py`):**
Trip fixture seeded. Three tests: 201 for same-org `context_ref_id`, 404 for
unknown/cross-org UUID, 422 for `general` context with `context_ref_id`.

## Key decisions

1. **Validate at create, not send.** Validating `context_ref_id` at conversation
   creation (not lazily at `send_message`) catches invalid references before any
   state is written. The backend owns validation of any input it introduces; T3
   then independently tests the scoping boundary.

2. **Prove threading at the API boundary.** `context_ref_id` threading was proven
   via direct `POST /conversations` with `context_ref_id` and a follow-up message,
   showing the entity flows through to the agent context. The frontend has no Trip
   or ExpenseReport detail routes yet, so end-to-end-through-UI is flagged (not
   built — no forward-pull into feature work).

3. **Persistence pattern held.** Agents/orchestrator remain flush-only;
   the single `db.commit()` stays at the HTTP endpoint. No change to
   `ai_agents/`; the one-way layering law (backend may import ai_agents,
   never the reverse) was not violated.

4. **Fix the citation mirror immediately.** The `policy/query` endpoint had the
   same `list(citations)` serialization bug as the one fixed in `streaming.py`.
   Fixed in the same commit rather than carrying a known crash.

## Verification

- **A1–A6 + B passed:** all 6 paths produced committed, durable, correctly-scoped
  `ai_decision_logs` rows confirmed from a separate DB connection.
- **Scope correctness:** `organization_id` and `user_id` on each row match the
  authenticated caller; entity-scoped conversation carried `context_ref_id` through.
- **Layering:** `grep -rn "import backend\|from backend" ai_agents/` → only the
  deferred `rag/reindex.py main()` import (pre-existing, test passes).
- **Drift guard:** `git diff --stat database/ ai_agents/` → empty.
- **Ruff + mypy:** clean on all changed backend modules.
- **Tests:** 67 backend passed, 326 ai_agents passed (393 total, 0 failures).

## Carried gaps (out of P6-T2 scope)

- **boto3 missing from container:** `POST /receipts` returns 500 on upload;
  receipt_analysis log-population is evidenced by 5 pre-existing `gap_fill` rows
  confirmed in DB + code-read of the delegation and log-write path. Live upload
  trigger blocked pending infra fix (add boto3 to container image).
- **policy_qa entity-scoping:** `context_type=policy_qa` with a `context_ref_id`
  is rejected with 422 ("not yet supported"). Full entity-scoping for policy
  conversations deferred to T3.
- **Intra-org record ownership (T3):** `validate_context_ref_id()` enforces
  org-membership; it does not enforce per-owner access for shared org resources.
  T3's adversarial suite should test whether an employee can open an
  AI conversation scoped to another employee's private expense report.
- **policy/query loosely-typed schemas:** cited output fields (`answer_type`,
  `answer`) are read with `.get()` defaults; no structured output contract yet.
- **NFR-SEC4 image serving:** pre-existing, not introduced by this task.
